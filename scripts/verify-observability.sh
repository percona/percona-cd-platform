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
#   scripts/verify-observability.sh                    # checks ps3.cd
#   scripts/verify-observability.sh cloud              # checks cloud.cd
#   scripts/verify-observability.sh ps3 --skip-master  # cluster-side only
#
# Read-only. Exits 0 if every stage is green, nonzero otherwise.
#
# Requires on PATH: aws, kubectl, curl, jq, dig, ssh (for master-side stages).
# Requires kubeconfig pointing at the percona-ci-platform EKS cluster.
# Requires AWS_PROFILE=percona-dev-admin or equivalent for the bearer fetch.

set -u
set -o pipefail

INST="${1:-ps3}"
SKIP_MASTER=0
case "${2:-}" in
  --skip-master) SKIP_MASTER=1 ;;
esac

PROFILE="${AWS_PROFILE:-percona-dev-admin}"
SECRET_ID="percona-ci-platform/alloy-gateway/bearer"
SECRET_REGION="us-east-1"
HOST="${INST}.cd.percona.com"
SSH_USER="ec2-user"
SSH_KEY="${HOME}/.ssh/id_rsa"

# Output helpers --------------------------------------------------------------
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

# Port-forward management -----------------------------------------------------
PF_PIDS=()
cleanup() {
  for pid in "${PF_PIDS[@]:-}"; do
    [ -n "$pid" ] && kill "$pid" 2>/dev/null || true
  done
  wait 2>/dev/null || true
}
trap cleanup EXIT

pf() {
  # pf <ns> <target> <local-port> <remote-port>
  local ns="$1" target="$2" lport="$3" rport="$4"
  kubectl -n "$ns" port-forward "$target" "${lport}:${rport}" >/dev/null 2>&1 &
  PF_PIDS+=("$!")
  sleep 2
}

require() {
  command -v "$1" >/dev/null 2>&1 || {
    printf "missing required tool: %s\n" "$1" >&2
    exit 2
  }
}
require curl
require jq
require kubectl
require dig

printf "verify-observability  master=%s  cluster-context=%s\n" \
  "$INST" "$(kubectl config current-context 2>/dev/null || echo '<no-kubeconfig>')"

# Stage 0: prerequisites ------------------------------------------------------
section "Stage 0: prerequisites"

if kubectl get ns alloy-gateway >/dev/null 2>&1; then
  stage_pass "kubectl context reaches cluster"
else
  stage_fail "kubectl context reaches cluster" "alloy-gateway namespace not found"
  exit 1
fi

if BEARER=$(aws secretsmanager get-secret-value \
    --secret-id "$SECRET_ID" --region "$SECRET_REGION" --profile "$PROFILE" \
    --query SecretString --output text 2>/dev/null \
    | jq -r '.bearer_token' 2>/dev/null) && [ -n "$BEARER" ]; then
  stage_pass "fetched alloy-gateway bearer from AWS SM" "len=${#BEARER}"
else
  BEARER=""
  stage_fail "fetched alloy-gateway bearer from AWS SM" "secret read failed (profile=$PROFILE)"
fi

# Stage 1: Hetzner plugin endpoint on master ---------------------------------
if [ "$SKIP_MASTER" = "0" ]; then
  section "Stage 1: Hetzner plugin endpoint (${HOST})"
  if SSHOUT=$(ssh -i "$SSH_KEY" -o ConnectTimeout=8 -o BatchMode=yes \
      "${SSH_USER}@${HOST}" '
        probe=$(curl -sLS -m 5 -o /tmp/hp.txt -w "%{http_code} %{size_download}" http://127.0.0.1:8080/hetzner-prometheus/)
        echo "PROBE=${probe}"
        ver=$(sudo python3 -c "
import zipfile, sys
try:
    with zipfile.ZipFile(\"/mnt/'"$INST"'.cd.percona.com/plugins/hetzner-cloud.jpi\") as z:
        with z.open(\"META-INF/MANIFEST.MF\") as f:
            for line in f.read().decode(errors=\"replace\").splitlines():
                if line.startswith(\"Plugin-Version:\"):
                    print(line.split(\":\",1)[1].strip()); break
except Exception:
    pass
" 2>/dev/null)
        echo "VERSION=${ver:-unknown}"
        fam=$(grep -c "^# HELP " /tmp/hp.txt)
        echo "FAMILIES=${fam:-0}"
      ' 2>/dev/null); then
    HTTP_CODE=$(echo "$SSHOUT" | awk -F= '/^PROBE=/ {print $2}' | awk '{print $1}')
    BYTES=$(echo "$SSHOUT"   | awk -F= '/^PROBE=/ {print $2}' | awk '{print $2}')
    PLUGIN_VER=$(echo "$SSHOUT" | awk -F= '/^VERSION=/ {print $2}')
    FAMILIES=$(echo "$SSHOUT"   | awk -F= '/^FAMILIES=/ {print $2}')
    if [ "$HTTP_CODE" = "200" ] && [ "${FAMILIES:-0}" -ge 1 ]; then
      stage_pass "plugin loopback /hetzner-prometheus/" \
        "v=${PLUGIN_VER:-?} HTTP=${HTTP_CODE} bytes=${BYTES} families=${FAMILIES}"
    else
      stage_fail "plugin loopback /hetzner-prometheus/" \
        "HTTP=${HTTP_CODE} bytes=${BYTES} families=${FAMILIES} v=${PLUGIN_VER:-?}"
    fi
  else
    stage_skip "plugin loopback /hetzner-prometheus/" "ssh ${HOST} failed"
  fi

  # Stage 2: Master Alloy systemd ------------------------------------------------
  section "Stage 2: Master Alloy systemd (${HOST})"
  if ALLOYOUT=$(ssh -i "$SSH_KEY" -o ConnectTimeout=8 -o BatchMode=yes \
      "${SSH_USER}@${HOST}" '
        systemctl is-active alloy
        curl -sS http://127.0.0.1:12345/metrics 2>/dev/null \
          | grep -E "^(prometheus_remote_storage_samples_total|prometheus_remote_storage_samples_failed_total|loki_write_sent_entries_total|loki_write_dropped_entries_total\{.*queue_is_full|prometheus_target_scrape_pool_targets)" \
          | head -10
      ' 2>/dev/null); then
    STATE=$(echo "$ALLOYOUT" | head -1)
    SAMPLES_OK=$(echo "$ALLOYOUT" | awk '/prometheus_remote_storage_samples_total/ && !/failed/ {print $2; exit}')
    SAMPLES_FAIL=$(echo "$ALLOYOUT" | awk '/prometheus_remote_storage_samples_failed_total/ {print $2; exit}')
    LOKI_OK=$(echo "$ALLOYOUT" | awk '/loki_write_sent_entries_total/ {print $2; exit}')
    if [ "$STATE" = "active" ] && [ "${SAMPLES_FAIL:-0}" -eq 0 ] 2>/dev/null \
       && [ "${SAMPLES_OK:-0}" -gt 0 ] 2>/dev/null && [ "${LOKI_OK:-0}" -gt 0 ] 2>/dev/null; then
      stage_pass "alloy systemd push state" \
        "active samples=${SAMPLES_OK%.*} failed=${SAMPLES_FAIL%.*} loki=${LOKI_OK%.*}"
    else
      stage_fail "alloy systemd push state" \
        "state=${STATE} samples=${SAMPLES_OK:-?} failed=${SAMPLES_FAIL:-?} loki=${LOKI_OK:-?}"
    fi
  else
    stage_skip "alloy systemd push state" "ssh ${HOST} failed"
  fi
else
  section "Stage 1+2: master-side checks (skipped)"
  stage_skip "plugin loopback" "--skip-master"
  stage_skip "alloy systemd"   "--skip-master"
fi

# Stage 3: ALB + bearer enforcement ------------------------------------------
section "Stage 3: ALB + bearer enforcement"
for spec in "mimir-push.cd.percona.com|/api/v1/metrics/write|application/x-protobuf" \
            "loki-push.cd.percona.com|/loki/api/v1/push|application/json" \
            "tempo-push.cd.percona.com|/v1/traces|application/x-protobuf"; do
  HOSTNAME="${spec%%|*}"; rest="${spec#*|}"
  PATHPART="${rest%%|*}"; CT="${rest##*|}"
  IP=$(dig +short "$HOSTNAME" 2>/dev/null | head -1)
  ANON=$(curl -sS -m 5 -o /dev/null -w "%{http_code}" -X POST -H "Content-Type: ${CT}" -d "" \
    "https://${HOSTNAME}${PATHPART}" 2>/dev/null)
  if [ "$ANON" = "401" ]; then
    stage_pass "${HOSTNAME} anonymous POST rejected" "ip=${IP} HTTP=401"
  else
    stage_fail "${HOSTNAME} anonymous POST rejected" "ip=${IP} HTTP=${ANON} (expected 401)"
  fi
done

if [ -n "$BEARER" ]; then
  NOW_NS="$(date +%s)000000000"
  PROBE_PAYLOAD="{\"streams\":[{\"stream\":{\"probe\":\"verify-${INST}-${NOW_NS}\"},\"values\":[[\"${NOW_NS}\",\"verify-observability probe\"]]}]}"
  AUTH=$(curl -sS -m 8 -o /dev/null -w "%{http_code}" -X POST \
    -H "Authorization: Bearer ${BEARER}" -H "Content-Type: application/json" \
    -d "$PROBE_PAYLOAD" \
    "https://loki-push.cd.percona.com/loki/api/v1/push" 2>/dev/null)
  if [ "$AUTH" = "204" ]; then
    stage_pass "loki-push bearer accepted" "HTTP=204"
  else
    stage_fail "loki-push bearer accepted" "HTTP=${AUTH} (expected 204)"
  fi
else
  stage_skip "loki-push bearer accepted" "no bearer fetched"
fi

# Stage 4: alloy-gateway pod state -------------------------------------------
section "Stage 4: alloy-gateway pods"
GW_POD=$(kubectl -n alloy-gateway get pods -l app.kubernetes.io/name=alloy \
  -o jsonpath='{.items[0].metadata.name}' 2>/dev/null)
GW_READY=$(kubectl -n alloy-gateway get pods -l app.kubernetes.io/name=alloy \
  --no-headers 2>/dev/null | awk '$2=="3/3" && $3=="Running"' | wc -l)
if [ -n "$GW_POD" ] && [ "$GW_READY" -ge 2 ]; then
  stage_pass "alloy-gateway pods Running" "ready_3of3=${GW_READY}"
else
  stage_fail "alloy-gateway pods Running" "pod=${GW_POD:-none} ready_3of3=${GW_READY}"
fi

GW_204=$(kubectl -n alloy-gateway logs "$GW_POD" -c nginx-auth --tail=200 2>/dev/null \
  | awk '/POST .* 204/ {n++} END {print n+0}')
if [ "${GW_204:-0}" -gt 0 ]; then
  stage_pass "nginx-auth recent 204s" "count=${GW_204}"
else
  stage_fail "nginx-auth recent 204s" "no 204s in last 200 log lines"
fi

# Stage 5: Mimir ingestion + canary query ------------------------------------
section "Stage 5: Mimir distributor + ingester + canary query"
pf mimir svc/mimir-distributor 28888 8080
RX_RAW=$(curl -sS -m 5 "http://localhost:28888/metrics" 2>/dev/null \
  | awk '/^cortex_distributor_received_samples_total/ {print $2}')
RX=$(printf '%.0f' "${RX_RAW:-0}" 2>/dev/null || echo 0)
if [ "${RX:-0}" -gt 0 ] 2>/dev/null; then
  stage_pass "mimir distributor received_samples" "total=${RX} (raw=${RX_RAW})"
else
  stage_fail "mimir distributor received_samples" "value=${RX_RAW:-?}"
fi

pf mimir svc/mimir-query-frontend 28080 8080
QURL="http://localhost:28080/prometheus/api/v1/query"
RES=$(curl -sS -m 8 -G --data-urlencode "query=count({master=\"${INST}.cd\"})" "$QURL" 2>/dev/null)
COUNT=$(echo "$RES" | jq -r '.data.result[0].value[1] // empty' 2>/dev/null)
if [ -n "$COUNT" ] && [ "$COUNT" -gt 0 ] 2>/dev/null; then
  stage_pass "mimir query master=${INST}.cd" "series=${COUNT}"
else
  stage_fail "mimir query master=${INST}.cd" "raw=${RES:0:160}"
fi

RES=$(curl -sS -m 8 -G --data-urlencode "query=hetzner_api_rate_limit_remaining{master=\"${INST}.cd\"}" "$QURL" 2>/dev/null)
RATE=$(echo "$RES" | jq -r '.data.result[0].value[1] // empty' 2>/dev/null)
if [ -n "$RATE" ]; then
  stage_pass "mimir query hetzner_api_rate_limit_remaining" "value=${RATE}"
else
  stage_fail "mimir query hetzner_api_rate_limit_remaining" "no value returned"
fi

# Stage 6: Loki ingestion + canary query -------------------------------------
section "Stage 6: Loki distributor + ingester ring + canary query"
pf loki svc/loki-distributor 23131 3100
LINES_RAW=$(curl -sS -m 5 "http://localhost:23131/metrics" 2>/dev/null \
  | awk '/^loki_distributor_lines_received_total\{.*tenant="fake"\}/ {print $2}')
LINES=$(printf '%.0f' "${LINES_RAW:-0}" 2>/dev/null || echo 0)
if [ "${LINES:-0}" -gt 0 ] 2>/dev/null; then
  stage_pass "loki distributor lines_received" "total=${LINES} (raw=${LINES_RAW})"
else
  stage_fail "loki distributor lines_received" "value=${LINES_RAW:-?}"
fi

RING=$(curl -sS -m 5 "http://localhost:23131/ring" 2>/dev/null)
ACTIVE=$(echo "$RING" | grep -oE 'ACTIVE' | wc -l)
UNHEALTHY=$(echo "$RING" | grep -oE 'UNHEALTHY' | wc -l)
if [ "$ACTIVE" -ge 3 ] && [ "$UNHEALTHY" -eq 0 ]; then
  stage_pass "loki ingester ring" "ACTIVE=${ACTIVE} UNHEALTHY=${UNHEALTHY}"
else
  stage_fail "loki ingester ring" "ACTIVE=${ACTIVE} UNHEALTHY=${UNHEALTHY} (expected ACTIVE>=3, UNHEALTHY=0)"
fi

pf loki svc/loki-query-frontend 23300 3100
START=$(($(date +%s) - 3600))000000000
END=$(date +%s)000000000
RES=$(curl -sS -m 12 -G \
  --data-urlencode "query={master=\"${INST}.cd\"}" \
  --data-urlencode "start=${START}" --data-urlencode "end=${END}" \
  --data-urlencode "limit=1" --data-urlencode "direction=BACKWARD" \
  "http://localhost:23300/loki/api/v1/query_range" 2>/dev/null)
LINE=$(echo "$RES" | jq -r '.data.result[0].values[0][1] // empty' 2>/dev/null)
if [ -n "$LINE" ]; then
  preview="${LINE:0:90}"
  stage_pass "loki query master=${INST}.cd" "got 1+ line: ${preview}…"
else
  stage_fail "loki query master=${INST}.cd" "no streams returned"
fi

# Stage 7: Grafana frontend ---------------------------------------------------
section "Stage 7: Grafana"
HEALTH=$(curl -sS -m 5 -o /dev/null -w "%{http_code}" "https://grafana.cd.percona.com/api/health" 2>/dev/null)
if [ "$HEALTH" = "200" ]; then
  stage_pass "grafana.cd.percona.com /api/health" "HTTP=200"
else
  stage_fail "grafana.cd.percona.com /api/health" "HTTP=${HEALTH}"
fi
G_READY=$(kubectl -n grafana get pods -l app.kubernetes.io/name=grafana \
  --no-headers 2>/dev/null | awk '$2 ~ /\// {split($2,p,"/"); if (p[1]==p[2] && $3=="Running") print}' | wc -l)
if [ "$G_READY" -ge 1 ]; then
  stage_pass "grafana pods Running" "ready=${G_READY}"
else
  stage_fail "grafana pods Running" "ready=${G_READY}"
fi

# Summary --------------------------------------------------------------------
printf "\n== Summary ==\n"
printf "  master=%s  pass=%d  fail=%d  skip=%d\n" "$INST" "$PASS" "$FAIL" "$SKIP"
if [ "$FAIL" -gt 0 ]; then
  exit 1
fi
exit 0
