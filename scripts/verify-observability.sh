#!/usr/bin/env bash
# verify-observability.sh - end-to-end health check of the LGTM push pipeline
# for a Jenkins master that runs the master-side Alloy systemd unit.
#
# All 10 masters now run the master-side Alloy unit (`Percona-Lab/jenkins-pipelines`
# PR #4037, commit 83adb97, merged 2026-05-10). For fleet-wide ingest spot-checks
# without SSH, use `scripts/check-master-ingest.sh` instead; this script remains
# the deepest per-master walk including the SSH-dependent master-side stages.
#
# Usage:
#   scripts/verify-observability.sh                        # checks ps3.cd
#   scripts/verify-observability.sh cloud                  # checks cloud.cd
#   scripts/verify-observability.sh ps3 --skip-master      # cluster-side only
#   scripts/verify-observability.sh --json-summary ps3     # append JSON envelope
#
# Read-only. Exits 0 if every stage is green, 1 on any FAIL, 2 on precondition.
#
# Required on PATH always: aws, curl, jq, kubectl, dig.
# Required on PATH when master stages are enabled: ssh.
# Requires a kubeconfig context for the LGTM cluster (default
# `percona-ci-platform`; override with $KUBE_CONTEXT). Requires
# AWS_PROFILE=percona-dev-admin or equivalent for the bearer fetch.
# Optional: $GRAFANA_TOKEN for the Grafana datasource + proxy probes
# (otherwise the script tries the `grafana` admin secret in-cluster;
# if neither is available those probes SKIP).

set -u
set -o pipefail

# Argument parsing -----------------------------------------------------------
INST=""
SKIP_MASTER=0
JSON_SUMMARY=0

usage() {
  cat <<EOF
verify-observability.sh - LGTM push pipeline health check
Usage: $0 [options] [<inst>]
  <inst>           master shortname (default: ps3); resolves to <inst>.cd.percona.com
  --skip-master    skip SSH-dependent master-side stages
  --json-summary   append a JSON envelope to stdout after the human summary
  -h, --help       show this help

Environment:
  KUBE_CONTEXT     kubectl context (default: percona-ci-platform)
  AWS_PROFILE      AWS profile for the bearer fetch (default: percona-dev-admin)
  GRAFANA_TOKEN    optional Grafana API token for datasource + proxy probes
EOF
}

while [[ $# -gt 0 ]]; do
  case "$1" in
    --skip-master)  SKIP_MASTER=1; shift ;;
    --json-summary) JSON_SUMMARY=1; shift ;;
    -h|--help)      usage; exit 0 ;;
    --*)            printf "unknown option: %s\n" "$1" >&2; usage >&2; exit 2 ;;
    *)              if [[ -z "$INST" ]]; then
                      INST="$1"; shift
                    else
                      printf "unexpected positional arg: %s\n" "$1" >&2
                      exit 2
                    fi ;;
  esac
done

INST="${INST:-ps3}"
KUBE_CONTEXT="${KUBE_CONTEXT:-percona-ci-platform}"
PROFILE="${AWS_PROFILE:-percona-dev-admin}"
SECRET_ID="percona-ci-platform/alloy-gateway/bearer"
SECRET_REGION="us-east-1"
HOST="${INST}.cd.percona.com"
SSH_USER="ec2-user"
SSH_KEY="${HOME}/.ssh/id_rsa"

# Output helpers -------------------------------------------------------------
PASS=0
FAIL=0
SKIP=0
RESULTS=()

green() { printf '\033[32m%s\033[0m' "$*"; }
red()   { printf '\033[31m%s\033[0m' "$*"; }
gray()  { printf '\033[90m%s\033[0m' "$*"; }

stage_pass() {
  local label="$1" detail="${2:-}"
  printf "  %s %s %s\n" "$(green "PASS")" "${label}" "$(gray "${detail}")"
  RESULTS+=("PASS|${label}|${detail}")
  PASS=$((PASS + 1))
}
stage_fail() {
  local label="$1" detail="${2:-}"
  printf "  %s %s %s\n" "$(red "FAIL")" "${label}" "${detail}"
  RESULTS+=("FAIL|${label}|${detail}")
  FAIL=$((FAIL + 1))
}
stage_skip() {
  local label="$1" detail="${2:-}"
  printf "  %s %s %s\n" "$(gray "SKIP")" "${label}" "$(gray "${detail}")"
  RESULTS+=("SKIP|${label}|${detail}")
  SKIP=$((SKIP + 1))
}
section() { printf "\n== %s ==\n" "$*"; }

# Pinned kubectl wrapper.
k() { kubectl --context "$KUBE_CONTEXT" "$@"; }

# Tool requirements ----------------------------------------------------------
require() {
  command -v "$1" >/dev/null 2>&1 || {
    printf "missing required tool: %s\n" "$1" >&2
    exit 2
  }
}
for t in aws curl jq kubectl dig; do require "$t"; done
if [[ "$SKIP_MASTER" = "0" ]]; then require ssh; fi

# Cleanup --------------------------------------------------------------------
PF_PIDS=()
PF_LOGS=()
# shellcheck disable=SC2329  # invoked via trap
cleanup() {
  local pid log
  for pid in "${PF_PIDS[@]:-}"; do
    [[ -n "${pid:-}" ]] && kill "$pid" 2>/dev/null || true
  done
  wait 2>/dev/null || true
  for log in "${PF_LOGS[@]:-}"; do
    [[ -n "${log:-}" && -f "$log" ]] && rm -f "$log" || true
  done
}
trap cleanup EXIT INT TERM

# Port-forward helpers -------------------------------------------------------
# pick_port: return a loopback port that is not currently bound. Best-effort
# free-port check using bash's /dev/tcp; race conditions are tolerable here
# because kubectl port-forward will surface a bind failure into the log file
# that the caller inspects.
pick_port() {
  local p tries=30
  for _ in $(seq 1 "$tries"); do
    p=$(( (RANDOM % 20000) + 30000 ))
    if ! (exec 3<>"/dev/tcp/127.0.0.1/${p}") 2>/dev/null; then
      printf '%s\n' "$p"
      return 0
    fi
    exec 3<&- 3>&-
  done
  return 1
}

# pf_start <ns> <target> <remote-port>
# Starts a kubectl port-forward in the background. On success prints the
# chosen loopback port. On failure returns nonzero and the caller MUST treat
# the dependent probe as unrunnable (the prior version silently leaked stale
# port-forwards onto fixed ports, which could route queries at the wrong
# backend).
pf_start() {
  local ns="$1" target="$2" rport="$3"
  local lport pid log
  if ! lport=$(pick_port); then
    return 1
  fi
  log=$(mktemp -t pf-XXXXXX) || return 1
  PF_LOGS+=("$log")
  k -n "$ns" port-forward "$target" "${lport}:${rport}" >/dev/null 2>"$log" &
  pid=$!
  PF_PIDS+=("$pid")
  for _ in $(seq 1 30); do
    if ! kill -0 "$pid" 2>/dev/null; then
      return 1
    fi
    if (exec 3<>"/dev/tcp/127.0.0.1/${lport}") 2>/dev/null; then
      exec 3<&- 3>&-
      printf '%s\n' "$lport"
      return 0
    fi
    sleep 0.5
  done
  return 1
}

# metric_sum <input> <metric-name>
# Sums the value of every line that starts with the given metric name and is
# followed by `{` or whitespace (so prefix-shared metric names do not
# cross-contaminate). Uses $NF, which is the value column in Prometheus
# exposition format (Alloy/Mimir/Loki do not emit timestamps).
metric_sum() {
  local input="$1" name="$2"
  printf '%s' "$input" | awk -v p="$name" '
    $0 !~ /^#/ {
      n = length(p)
      if (substr($0, 1, n) == p) {
        nxt = substr($0, n + 1, 1)
        if (nxt == "{" || nxt == " " || nxt == "\t") { s += $NF }
      }
    }
    END { printf("%.0f\n", s + 0) }
  '
}

# Render the summary block and (optionally) the JSON envelope, then exit.
finalize() {
  local code="${1:-0}"
  printf "\n== Summary ==\n"
  printf "  master=%s  context=%s  pass=%d  fail=%d  skip=%d\n" \
    "$INST" "$KUBE_CONTEXT" "$PASS" "$FAIL" "$SKIP"
  if [[ "$FAIL" -gt 0 || "$SKIP" -gt 0 ]]; then
    printf "\n== Issues ==\n"
    local r status rest label detail
    for r in "${RESULTS[@]:-}"; do
      [[ -z "${r:-}" ]] && continue
      status="${r%%|*}"; rest="${r#*|}"
      label="${rest%%|*}"; detail="${rest#*|}"
      case "$status" in
        FAIL) printf "  %s %s %s\n" "$(red FAIL)" "$label" "$detail" ;;
        SKIP) printf "  %s %s %s\n" "$(gray SKIP)" "$label" "$(gray "$detail")" ;;
      esac
    done
  fi
  if [[ "$JSON_SUMMARY" = "1" ]]; then
    printf "\n== JSON Summary ==\n"
    local r status rest label detail entries=()
    for r in "${RESULTS[@]:-}"; do
      [[ -z "${r:-}" ]] && continue
      status="${r%%|*}"; rest="${r#*|}"
      label="${rest%%|*}"; detail="${rest#*|}"
      entries+=("$(jq -nc \
        --arg s "$status" --arg l "$label" --arg d "$detail" \
        '{status:$s,label:$l,detail:$d}')")
    done
    local results_json
    if [[ "${#entries[@]}" -gt 0 ]]; then
      results_json=$(printf '%s\n' "${entries[@]}" | jq -s '.')
    else
      results_json='[]'
    fi
    jq -n \
      --arg master   "$INST" \
      --arg context  "$KUBE_CONTEXT" \
      --argjson pass "$PASS" \
      --argjson fail "$FAIL" \
      --argjson skip "$SKIP" \
      --argjson results "$results_json" \
      '{master:$master,context:$context,pass:$pass,fail:$fail,skip:$skip,results:$results}'
  fi
  exit "$code"
}

printf "verify-observability  master=%s  cluster-context=%s\n" \
  "$INST" "$KUBE_CONTEXT"

# Stage 0: prerequisites -----------------------------------------------------
section "Stage 0: prerequisites"

if k get ns alloy-gateway >/dev/null 2>&1; then
  stage_pass "kubectl context reaches cluster" "ctx=${KUBE_CONTEXT}"
else
  stage_fail "kubectl context reaches cluster" \
    "context=${KUBE_CONTEXT}; namespace alloy-gateway not found"
  finalize 2
fi

# Bearer fetch: separate operator-precondition (AWS auth) from production
# config error (secret exists but .bearer_token missing).
BEARER=""
SECRET_JSON=""
if SECRET_JSON=$(aws secretsmanager get-secret-value \
      --secret-id "$SECRET_ID" --region "$SECRET_REGION" --profile "$PROFILE" \
      --query SecretString --output text 2>/dev/null); then
  BEARER=$(printf '%s' "$SECRET_JSON" | jq -r '.bearer_token // empty' 2>/dev/null || true)
  if [[ -n "$BEARER" && "$BEARER" != "null" ]]; then
    stage_pass "fetched alloy-gateway bearer from AWS SM" "len=${#BEARER}"
  else
    stage_fail "fetched alloy-gateway bearer from AWS SM" \
      "secret exists but .bearer_token missing/null (production config error)"
    BEARER=""
  fi
else
  stage_skip "fetched alloy-gateway bearer from AWS SM" \
    "AWS SM read failed (profile=$PROFILE); bearer-dependent stages will SKIP"
fi

# Stage 1+2: master-side (SSH-dependent) -------------------------------------
SSH_REACHABLE=0
if [[ "$SKIP_MASTER" = "1" ]]; then
  section "Stage 1+2: master-side checks (skipped)"
  stage_skip "plugin loopback"  "--skip-master"
  stage_skip "alloy systemd"    "--skip-master"
elif [[ ! -r "$SSH_KEY" ]]; then
  section "Stage 1+2: master-side checks (skipped)"
  stage_skip "plugin loopback"  "ssh key not present on this host ($SSH_KEY)"
  stage_skip "alloy systemd"    "ssh key not present on this host ($SSH_KEY)"
else
  section "Stage 1: SSH transport to master (${HOST})"
  if ssh -i "$SSH_KEY" -o ConnectTimeout=8 -o BatchMode=yes \
       "${SSH_USER}@${HOST}" 'true' >/dev/null 2>&1; then
    stage_pass "ssh transport to ${HOST}" ""
    SSH_REACHABLE=1
  else
    stage_fail "ssh transport to ${HOST}" \
      "ssh key present but connect failed; loopback probes will SKIP"
  fi

  if [[ "$SSH_REACHABLE" = "1" ]]; then
    section "Stage 1: Hetzner plugin endpoint (${HOST})"
    # Remote script via stdin so we can use a literal-quoted heredoc with
    # mktemp + trap on the master. The local INST is passed as the first
    # positional arg ($1 on the remote side) instead of being interpolated
    # client-side; this keeps the heredoc body free of client-shell
    # substitution surprises.
    if SSHOUT=$(ssh -i "$SSH_KEY" -o ConnectTimeout=8 -o BatchMode=yes \
        "${SSH_USER}@${HOST}" "bash -s ${INST}" 2>/dev/null <<'REMOTE'
set -u
inst="$1"
tmp=$(mktemp)
trap 'rm -f "$tmp"' EXIT
probe=$(curl -sLS -m 5 -o "$tmp" -w '%{http_code} %{size_download}' \
  http://127.0.0.1:8080/hetzner-prometheus/)
echo "PROBE=${probe}"
ver=$(sudo python3 - "$inst" <<'PY' 2>/dev/null
import sys, zipfile
inst = sys.argv[1]
try:
    with zipfile.ZipFile(f"/mnt/{inst}.cd.percona.com/plugins/hetzner-cloud.jpi") as z:
        with z.open("META-INF/MANIFEST.MF") as f:
            for line in f.read().decode(errors="replace").splitlines():
                if line.startswith("Plugin-Version:"):
                    print(line.split(":", 1)[1].strip())
                    break
except Exception:
    pass
PY
)
echo "VERSION=${ver:-unknown}"
fam=$(grep -c '^# HELP ' "$tmp" || true)
echo "FAMILIES=${fam:-0}"
REMOTE
        ); then
      HTTP_CODE=$(echo "$SSHOUT" | awk -F= '/^PROBE=/ {print $2}' | awk '{print $1}')
      BYTES=$(echo "$SSHOUT"     | awk -F= '/^PROBE=/ {print $2}' | awk '{print $2}')
      PLUGIN_VER=$(echo "$SSHOUT" | awk -F= '/^VERSION=/ {print $2}')
      FAMILIES=$(echo "$SSHOUT"   | awk -F= '/^FAMILIES=/ {print $2}')
      if [[ "$HTTP_CODE" = "200" && "${FAMILIES:-0}" -ge 1 ]]; then
        stage_pass "plugin loopback /hetzner-prometheus/" \
          "v=${PLUGIN_VER:-?} HTTP=${HTTP_CODE} bytes=${BYTES} families=${FAMILIES}"
      else
        stage_fail "plugin loopback /hetzner-prometheus/" \
          "HTTP=${HTTP_CODE} bytes=${BYTES} families=${FAMILIES} v=${PLUGIN_VER:-?}"
      fi
    else
      stage_fail "plugin loopback /hetzner-prometheus/" \
        "ssh probe command failed on ${HOST}"
    fi

    section "Stage 2: Master Alloy systemd (${HOST})"
    # Sample localhost:12345/metrics twice ~12s apart so we report deltas,
    # not lifetime counters. A historical blip used to peg failed_total > 0
    # forever; this delta semantics is what gotcha 16 in the skill calls
    # for. The remote uses a function defined inline to filter labelled
    # series and we transmit two metric blobs separated by a delimiter.
    if ALLOYOUT=$(ssh -i "$SSH_KEY" -o ConnectTimeout=8 -o BatchMode=yes \
         "${SSH_USER}@${HOST}" bash -s 2>/dev/null <<'REMOTE'
set -u
state=$(systemctl is-active alloy 2>/dev/null || echo unknown)
echo "STATE=${state}"
echo "----SNAPSHOT-A----"
curl -sS -m 5 http://127.0.0.1:12345/metrics 2>/dev/null || true
echo "----SLEEP----"
sleep 12
echo "----SNAPSHOT-B----"
curl -sS -m 5 http://127.0.0.1:12345/metrics 2>/dev/null || true
REMOTE
        ); then
      STATE=$(echo "$ALLOYOUT" | awk -F= '/^STATE=/ {print $2; exit}')
      SNAP_A=$(echo "$ALLOYOUT" | awk '
        /^----SNAPSHOT-A----$/ {a=1; next}
        /^----SLEEP----$/      {a=0}
        a {print}
      ')
      SNAP_B=$(echo "$ALLOYOUT" | awk '
        /^----SNAPSHOT-B----$/ {b=1; next}
        b {print}
      ')
      SENT_A=$(metric_sum "$SNAP_A" prometheus_remote_storage_samples_total)
      SENT_B=$(metric_sum "$SNAP_B" prometheus_remote_storage_samples_total)
      FAIL_A=$(metric_sum "$SNAP_A" prometheus_remote_storage_samples_failed_total)
      FAIL_B=$(metric_sum "$SNAP_B" prometheus_remote_storage_samples_failed_total)
      DROP_A=$(metric_sum "$SNAP_A" prometheus_remote_storage_samples_dropped_total)
      DROP_B=$(metric_sum "$SNAP_B" prometheus_remote_storage_samples_dropped_total)
      LOKI_A=$(metric_sum "$SNAP_A" loki_write_sent_entries_total)
      LOKI_B=$(metric_sum "$SNAP_B" loki_write_sent_entries_total)
      PEND_B=$(metric_sum "$SNAP_B" prometheus_remote_storage_samples_pending)
      D_SENT=$(( SENT_B - SENT_A ))
      D_FAIL=$(( FAIL_B - FAIL_A ))
      D_DROP=$(( DROP_B - DROP_A ))
      D_LOKI=$(( LOKI_B - LOKI_A ))
      if [[ "$STATE" = "active" && "$D_SENT" -gt 0 \
            && "$D_FAIL" -eq 0 && "$D_DROP" -eq 0 ]]; then
        stage_pass "alloy systemd push state" \
          "active dsent=${D_SENT} dfailed=${D_FAIL} ddropped=${D_DROP} dloki=${D_LOKI} pending=${PEND_B}"
      else
        stage_fail "alloy systemd push state" \
          "state=${STATE} dsent=${D_SENT} dfailed=${D_FAIL} ddropped=${D_DROP} dloki=${D_LOKI} pending=${PEND_B}"
      fi
    else
      stage_fail "alloy systemd push state" "ssh probe command failed on ${HOST}"
    fi
  else
    # ssh transport already FAILed; do not also fail dependent probes.
    stage_skip "plugin loopback /hetzner-prometheus/" "ssh transport unavailable"
    stage_skip "alloy systemd push state"             "ssh transport unavailable"
  fi
fi

# Stage 3: ALB + bearer enforcement ------------------------------------------
section "Stage 3: ALB + bearer enforcement"
for spec in "mimir-push.cd.percona.com|/api/v1/metrics/write|application/x-protobuf" \
            "loki-push.cd.percona.com|/loki/api/v1/push|application/json" \
            "tempo-push.cd.percona.com|/v1/traces|application/x-protobuf"; do
  HOSTNAME="${spec%%|*}"; rest="${spec#*|}"
  PATHPART="${rest%%|*}"; CT="${rest##*|}"
  IP=$(dig +short "$HOSTNAME" 2>/dev/null | head -1)
  ANON=$(curl -sS -m 5 -o /dev/null -w "%{http_code}" -X POST \
    -H "Content-Type: ${CT}" -d "" \
    "https://${HOSTNAME}${PATHPART}" 2>/dev/null)
  if [[ "$ANON" = "401" ]]; then
    stage_pass "${HOSTNAME} anonymous POST rejected" "ip=${IP} HTTP=401"
  else
    stage_fail "${HOSTNAME} anonymous POST rejected" "ip=${IP} HTTP=${ANON} (expected 401)"
  fi
done

NOW_NS="$(date +%s)000000000"
PROBE_LABEL="verify-${INST}-${NOW_NS}"
if [[ -n "$BEARER" ]]; then
  PROBE_PAYLOAD="{\"streams\":[{\"stream\":{\"probe\":\"${PROBE_LABEL}\"},\"values\":[[\"${NOW_NS}\",\"verify-observability probe\"]]}]}"
  AUTH=$(curl -sS -m 8 -o /dev/null -w "%{http_code}" -X POST \
    -H "Authorization: Bearer ${BEARER}" -H "Content-Type: application/json" \
    -d "$PROBE_PAYLOAD" \
    "https://loki-push.cd.percona.com/loki/api/v1/push" 2>/dev/null)
  if [[ "$AUTH" = "204" ]]; then
    stage_pass "loki-push bearer accepted" "HTTP=204"
  else
    stage_fail "loki-push bearer accepted" "HTTP=${AUTH} (expected 204)"
  fi

  # Tempo coverage: until real traces exist, at least assert that an
  # authenticated POST with an empty body is no longer rejected as
  # unauthenticated (any 2xx/4xx/5xx other than 401 satisfies the auth
  # gate). 401 here would mean the bearer-auth sidecar is rejecting valid
  # credentials for the tempo path.
  TANON=$(curl -sS -m 8 -o /dev/null -w "%{http_code}" -X POST \
    -H "Authorization: Bearer ${BEARER}" -H "Content-Type: application/x-protobuf" \
    -d "" \
    "https://tempo-push.cd.percona.com/v1/traces" 2>/dev/null)
  if [[ "$TANON" != "401" && -n "$TANON" ]]; then
    stage_pass "tempo-push bearer not 401" "HTTP=${TANON}"
  else
    stage_fail "tempo-push bearer not 401" "HTTP=${TANON} (bearer rejected)"
  fi
else
  stage_skip "loki-push bearer accepted" "no bearer fetched"
  stage_skip "tempo-push bearer not 401" "no bearer fetched"
fi

# Stage 4: alloy-gateway pod state -------------------------------------------
section "Stage 4: alloy-gateway pods"
GW_DEPLOY_JSON=$(k -n alloy-gateway get deploy alloy-gateway -o json 2>/dev/null || echo '{}')
GW_DESIRED=$(echo "$GW_DEPLOY_JSON" | jq -r '.spec.replicas // 0')
GW_READY_REPLICAS=$(echo "$GW_DEPLOY_JSON" | jq -r '.status.readyReplicas // 0')
GW_AVAIL=$(echo "$GW_DEPLOY_JSON" | jq -r '
  .status.conditions[]? | select(.type=="Available") | .status' 2>/dev/null)

if [[ "$GW_DESIRED" -gt 0 && "$GW_READY_REPLICAS" -eq "$GW_DESIRED" && "$GW_AVAIL" = "True" ]]; then
  stage_pass "alloy-gateway deploy Ready" \
    "ready=${GW_READY_REPLICAS}/${GW_DESIRED} Available=True"
else
  stage_fail "alloy-gateway deploy Ready" \
    "ready=${GW_READY_REPLICAS}/${GW_DESIRED} Available=${GW_AVAIL:-?}"
fi

# Aggregate nginx-auth logs across ALL gateway pods, only since the script
# started, so we never count stale log lines from a prior incident window.
SCRIPT_START_RFC3339="$(date -u -d '5 minutes ago' +%Y-%m-%dT%H:%M:%SZ 2>/dev/null || \
                       date -u -v-5M +%Y-%m-%dT%H:%M:%SZ 2>/dev/null || \
                       date -u +%Y-%m-%dT%H:%M:%SZ)"
GW_PODS=$(k -n alloy-gateway get pods -l app.kubernetes.io/name=alloy \
  -o jsonpath='{.items[*].metadata.name}' 2>/dev/null)
GW_204=0
if [[ -n "$GW_PODS" ]]; then
  for p in $GW_PODS; do
    n=$(k -n alloy-gateway logs "$p" -c nginx-auth \
        --since-time="$SCRIPT_START_RFC3339" --tail=1000 2>/dev/null \
        | awk '/POST .* 204/ {n++} END {print n+0}')
    GW_204=$(( GW_204 + ${n:-0} ))
  done
fi
if [[ "$GW_204" -gt 0 ]]; then
  stage_pass "nginx-auth recent 204s" "count=${GW_204} across ${GW_DESIRED} pod(s)"
else
  stage_fail "nginx-auth recent 204s" \
    "no 204s since ${SCRIPT_START_RFC3339} across ${GW_DESIRED} pod(s)"
fi

# Stage 5: Mimir ingestion + canary query ------------------------------------
section "Stage 5: Mimir distributor + ingester + canary query"
if M_DIST_PORT=$(pf_start mimir svc/mimir-distributor 8080); then
  M_METRICS=$(curl -sS -m 5 "http://localhost:${M_DIST_PORT}/metrics" 2>/dev/null || true)
  RX=$(metric_sum "$M_METRICS" cortex_distributor_received_samples_total)
  if [[ "${RX:-0}" -gt 0 ]]; then
    stage_pass "mimir distributor received_samples" "total=${RX}"
  else
    stage_fail "mimir distributor received_samples" "sum=${RX:-?}"
  fi
else
  stage_fail "mimir distributor received_samples" "port-forward to mimir-distributor failed"
fi

if M_QF_PORT=$(pf_start mimir svc/mimir-query-frontend 8080); then
  QURL="http://localhost:${M_QF_PORT}/prometheus/api/v1/query"
  RES=$(curl -sS -m 8 -G --data-urlencode "query=count({master=\"${INST}.cd\"})" "$QURL" 2>/dev/null)
  COUNT=$(echo "$RES" | jq -r '.data.result[0].value[1] // empty' 2>/dev/null)
  if [[ -n "$COUNT" && "$COUNT" -gt 0 ]] 2>/dev/null; then
    stage_pass "mimir query master=${INST}.cd" "series=${COUNT}"
  else
    stage_fail "mimir query master=${INST}.cd" "raw=${RES:0:160}"
  fi

  RES=$(curl -sS -m 8 -G \
    --data-urlencode "query=hetzner_api_rate_limit_remaining{master=\"${INST}.cd\"}" \
    "$QURL" 2>/dev/null)
  RATE=$(echo "$RES" | jq -r '.data.result[0].value[1] // empty' 2>/dev/null)
  if [[ -n "$RATE" ]]; then
    stage_pass "mimir query hetzner_api_rate_limit_remaining" "value=${RATE}"
  else
    stage_fail "mimir query hetzner_api_rate_limit_remaining" "no value returned"
  fi
else
  stage_fail "mimir query master=${INST}.cd" "port-forward to mimir-query-frontend failed"
  stage_fail "mimir query hetzner_api_rate_limit_remaining" "port-forward to mimir-query-frontend failed"
fi

# Stage 6: Loki ingestion + canary query -------------------------------------
section "Stage 6: Loki distributor + ingester ring + canary query"
LOKI_EXPECTED_INGESTERS=$(k -n loki get sts -l app.kubernetes.io/component=ingester \
  -o json 2>/dev/null | jq -r '[.items[].spec.replicas] | add // 0')
LOKI_EXPECTED_INGESTERS="${LOKI_EXPECTED_INGESTERS:-0}"

if L_DIST_PORT=$(pf_start loki svc/loki-distributor 3100); then
  L_METRICS=$(curl -sS -m 5 "http://localhost:${L_DIST_PORT}/metrics" 2>/dev/null || true)
  LINES=$(metric_sum "$L_METRICS" loki_distributor_lines_received_total)
  if [[ "${LINES:-0}" -gt 0 ]]; then
    stage_pass "loki distributor lines_received" "total=${LINES}"
  else
    stage_fail "loki distributor lines_received" "sum=${LINES:-?}"
  fi

  RING_RAW=$(curl -sS -m 5 "http://localhost:${L_DIST_PORT}/ring?format=json" 2>/dev/null)
  if RING_STATES=$(echo "$RING_RAW" | jq -r '.shards[]?.state // empty' 2>/dev/null) \
     && [[ -n "$RING_STATES" ]]; then
    ACTIVE=$(printf '%s\n' "$RING_STATES" | grep -cx ACTIVE || true)
    UNHEALTHY=$(printf '%s\n' "$RING_STATES" | grep -cx UNHEALTHY || true)
  else
    # Fallback for older /ring HTML endpoint.
    RING_HTML=$(curl -sS -m 5 "http://localhost:${L_DIST_PORT}/ring" 2>/dev/null || true)
    ACTIVE=$(printf '%s' "$RING_HTML" | grep -oE 'ACTIVE' | wc -l)
    UNHEALTHY=$(printf '%s' "$RING_HTML" | grep -oE 'UNHEALTHY' | wc -l)
  fi
  if [[ "$LOKI_EXPECTED_INGESTERS" -gt 0 \
        && "$ACTIVE" -eq "$LOKI_EXPECTED_INGESTERS" \
        && "$UNHEALTHY" -eq 0 ]]; then
    stage_pass "loki ingester ring" \
      "ACTIVE=${ACTIVE}/${LOKI_EXPECTED_INGESTERS} UNHEALTHY=${UNHEALTHY}"
  else
    stage_fail "loki ingester ring" \
      "ACTIVE=${ACTIVE}/${LOKI_EXPECTED_INGESTERS} UNHEALTHY=${UNHEALTHY} (expected ACTIVE=${LOKI_EXPECTED_INGESTERS}, UNHEALTHY=0)"
  fi
else
  stage_fail "loki distributor lines_received" "port-forward to loki-distributor failed"
  stage_fail "loki ingester ring"              "port-forward to loki-distributor failed"
fi

if L_QF_PORT=$(pf_start loki svc/loki-query-frontend 3100); then
  # Probe round-trip: query for the bearer-pushed probe from Stage 3.
  # This is a deterministic end-to-end check (gateway -> distributor ->
  # ingester -> query-frontend) that does not depend on master activity.
  if [[ -n "$BEARER" ]]; then
    sleep 2  # allow ingester to index
    PROBE_START=$((NOW_NS - 60000000000))
    PROBE_END=$((NOW_NS + 60000000000))
    RES=$(curl -sS -m 12 -G \
      --data-urlencode "query={probe=\"${PROBE_LABEL}\"}" \
      --data-urlencode "start=${PROBE_START}" --data-urlencode "end=${PROBE_END}" \
      --data-urlencode "limit=1" --data-urlencode "direction=BACKWARD" \
      "http://localhost:${L_QF_PORT}/loki/api/v1/query_range" 2>/dev/null)
    PROBE_LINE=$(echo "$RES" | jq -r '.data.result[0].values[0][1] // empty' 2>/dev/null)
    if [[ -n "$PROBE_LINE" ]]; then
      stage_pass "loki probe round-trip" "probe=${PROBE_LABEL} returned"
    else
      stage_fail "loki probe round-trip" "no streams for probe=${PROBE_LABEL}"
    fi
  else
    stage_skip "loki probe round-trip" "no bearer fetched"
  fi

  # Master-side coverage: any log line from this master in the last 24h.
  # A 1h window was too strict for low-volume masters (ps80 emits ~1-200
  # lines/hour at idle, mostly GitHub OAuth cookie warnings). The pipeline
  # is already validated by the probe round-trip above; this check only
  # confirms that the master-side Alloy systemd unit has reached Loki at
  # some point in the recent past.
  START=$(($(date +%s) - 86400))000000000
  END=$(date +%s)000000000
  RES=$(curl -sS -m 12 -G \
    --data-urlencode "query={master=\"${INST}.cd\"}" \
    --data-urlencode "start=${START}" --data-urlencode "end=${END}" \
    --data-urlencode "limit=1" --data-urlencode "direction=BACKWARD" \
    "http://localhost:${L_QF_PORT}/loki/api/v1/query_range" 2>/dev/null)
  LINE=$(echo "$RES" | jq -r '.data.result[0].values[0][1] // empty' 2>/dev/null)
  LAST_TS=$(echo "$RES" | jq -r '.data.result[0].values[0][0] // empty' 2>/dev/null)
  if [[ -n "$LINE" ]]; then
    ts_int=$(echo "$LAST_TS" | head -c10)
    age=$(( $(date +%s) - ts_int ))
    preview="${LINE:0:80}"
    stage_pass "loki query master=${INST}.cd (24h)" "age=${age}s: ${preview}…"
  else
    stage_fail "loki query master=${INST}.cd (24h)" "no streams in last 24h"
  fi
else
  stage_fail "loki probe round-trip"              "port-forward to loki-query-frontend failed"
  stage_fail "loki query master=${INST}.cd (24h)" "port-forward to loki-query-frontend failed"
fi

# Stage 7: Grafana frontend + datasource proxy -------------------------------
section "Stage 7: Grafana"
HEALTH=$(curl -sS -m 5 -o /dev/null -w "%{http_code}" "https://grafana.cd.percona.com/api/health" 2>/dev/null)
if [[ "$HEALTH" = "200" ]]; then
  stage_pass "grafana.cd.percona.com /api/health" "HTTP=200"
else
  stage_fail "grafana.cd.percona.com /api/health" "HTTP=${HEALTH}"
fi

G_READY=$(k -n grafana get pods -l app.kubernetes.io/name=grafana -o json 2>/dev/null \
  | jq '[.items[] | select(.status.phase=="Running") | select(
       [.status.containerStatuses[]?.ready] | all)] | length')
G_READY="${G_READY:-0}"
if [[ "$G_READY" -ge 1 ]]; then
  stage_pass "grafana pods Running" "ready=${G_READY}"
else
  stage_fail "grafana pods Running" "ready=${G_READY}"
fi

# Datasource proxy + UID assertions, auth-optional. Prefer $GRAFANA_TOKEN;
# else try the in-cluster `grafana` admin secret. If neither is available,
# SKIP cleanly (incident operators without Grafana credentials should still
# get a green run from the other stages).
G_AUTH=""
if [[ -n "${GRAFANA_TOKEN:-}" ]]; then
  G_AUTH="bearer"
elif G_PASS=$(k -n grafana get secret grafana \
                -o jsonpath='{.data.admin-password}' 2>/dev/null | base64 -d 2>/dev/null) \
     && [[ -n "$G_PASS" ]]; then
  G_USER=$(k -n grafana get secret grafana \
            -o jsonpath='{.data.admin-user}' 2>/dev/null | base64 -d 2>/dev/null || echo admin)
  G_USER="${G_USER:-admin}"
  G_AUTH="basic"
fi

g_curl() {
  if [[ "$G_AUTH" = "bearer" ]]; then
    curl -sS -m 8 -H "Authorization: Bearer ${GRAFANA_TOKEN}" "$@"
  else
    curl -sS -m 8 -u "${G_USER}:${G_PASS}" "$@"
  fi
}

if [[ -n "$G_AUTH" ]]; then
  # Probe whether the credentials actually authenticate (Grafana behind
  # Authentik OIDC will reject local admin basic-auth with 401 even when
  # the secret exists in-cluster). If auth doesn't work, SKIP cleanly
  # rather than FAIL the operator.
  G_PROBE_CODE=$(g_curl -o /dev/null -w "%{http_code}" \
    "https://grafana.cd.percona.com/api/user" 2>/dev/null || echo "000")
  if [[ "$G_PROBE_CODE" = "200" ]]; then
    DS_JSON=$(g_curl "https://grafana.cd.percona.com/api/datasources" 2>/dev/null || echo '[]')
    for uid in mimir loki tempo; do
      found=$(echo "$DS_JSON" | jq -r --arg u "$uid" '.[] | select(.uid==$u) | .uid' 2>/dev/null)
      if [[ "$found" = "$uid" ]]; then
        stage_pass "grafana datasource uid=${uid}" "present"
      else
        stage_fail "grafana datasource uid=${uid}" "uid not found in /api/datasources"
      fi
    done
    PQ=$(g_curl -G --data-urlencode "query=vector(1)" \
      "https://grafana.cd.percona.com/api/datasources/proxy/uid/mimir/api/v1/query" 2>/dev/null)
    PQV=$(echo "$PQ" | jq -r '.data.result[0].value[1] // empty' 2>/dev/null)
    if [[ "$PQV" = "1" ]]; then
      stage_pass "grafana mimir proxy query" "vector(1)=1"
    else
      stage_fail "grafana mimir proxy query" "raw=${PQ:0:160}"
    fi
  else
    skip_reason="auth probe /api/user HTTP=${G_PROBE_CODE} (Grafana behind OIDC?)"
    stage_skip "grafana datasource uid=mimir"  "$skip_reason"
    stage_skip "grafana datasource uid=loki"   "$skip_reason"
    stage_skip "grafana datasource uid=tempo"  "$skip_reason"
    stage_skip "grafana mimir proxy query"     "$skip_reason"
  fi
else
  stage_skip "grafana datasource uid=mimir"  "no GRAFANA_TOKEN and no admin secret readable"
  stage_skip "grafana datasource uid=loki"   "no GRAFANA_TOKEN and no admin secret readable"
  stage_skip "grafana datasource uid=tempo"  "no GRAFANA_TOKEN and no admin secret readable"
  stage_skip "grafana mimir proxy query"     "no GRAFANA_TOKEN and no admin secret readable"
fi

# Summary --------------------------------------------------------------------
if [[ "$FAIL" -gt 0 ]]; then
  finalize 1
else
  finalize 0
fi
