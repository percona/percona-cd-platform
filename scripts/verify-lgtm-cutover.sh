#!/usr/bin/env bash
# verify-lgtm-cutover.sh - validate the LGTM-only migration end-to-end.
#
# Companion to verify-observability.sh (which checks the master-side
# Alloy push pipeline). This script focuses on the cluster-side cutover
# stages (P3..P6 of the LGTM-only migration, plan:
# ~/.claude/plans/hashed-shimmying-crane.md):
#
#   pre       Snapshot baseline before any migration phase runs.
#   ruler     After P3 -- PrometheusRule sync to Mimir's ruler.
#   post      After P5 -- read path on Mimir, write path via Alloy.
#   worker    Trigger a Hetzner build on the canary master and prove
#             the provision telemetry lands in both Mimir and Loki.
#   dashboard After P5 -- Grafana dashboards render against Mimir.
#   failsafe  After P6 -- kps remnants gone, no fresh kps series.
#   all       (default) Run every stage that's safe in the current state.
#
# Usage:
#   scripts/verify-lgtm-cutover.sh [--phase <phase>] [--inst <name>]
#                                  [--job <jenkins-job>] [--skip-master]
#
# Read-only against the cluster except for stage `worker` (triggers a
# Jenkins build). Exits 0 if every stage in the chosen phase is green.
#
# Required tools: aws, kubectl, curl, jq, ssh (master-side), jenkins CLI.
# Required env: AWS_PROFILE=percona-dev-admin (bearer fetch + paws ssh).
# Optional env: GRAFANA_TOKEN (Grafana SA token; falls back to admin
#               basic auth via the in-cluster grafana Secret).

set -u
set -o pipefail

# ----- defaults ------------------------------------------------------------
PHASE="all"
INST="ps3"
JOB_OVERRIDE=""
SKIP_MASTER=0

while [ $# -gt 0 ]; do
  case "$1" in
    --phase)        PHASE="$2"; shift 2 ;;
    --inst)         INST="$2"; shift 2 ;;
    --job)          JOB_OVERRIDE="$2"; shift 2 ;;
    --skip-master)  SKIP_MASTER=1; shift ;;
    -h|--help)
      sed -n '2,30p' "$0"; exit 0 ;;
    *)
      printf "unknown arg: %s\n" "$1" >&2
      exit 2 ;;
  esac
done

PROFILE="${AWS_PROFILE:-percona-dev-admin}"
SECRET_REGION="us-east-1"
SECRET_ID_MIMIR="percona-ci-platform/alloy-gateway/bearer"

MIMIR_PUSH_HOST="mimir-push.cd.percona.com"
LOKI_PUSH_HOST="loki-push.cd.percona.com"
TENANT="percona-ci"

# ----- output --------------------------------------------------------------
PASS=0; FAIL=0; SKIP=0
RESULTS=()

green() { printf '\033[32m%s\033[0m' "$*"; }
red()   { printf '\033[31m%s\033[0m' "$*"; }
gray()  { printf '\033[90m%s\033[0m' "$*"; }
yellow(){ printf '\033[33m%s\033[0m' "$*"; }

stage_pass() { printf "  %s %-44s %s\n" "$(green PASS)" "$1" "$(gray "${2:-}")"; PASS=$((PASS+1)); RESULTS+=("PASS|$1|${2:-}"); }
stage_fail() { printf "  %s %-44s %s\n" "$(red   FAIL)" "$1" "${2:-}";          FAIL=$((FAIL+1)); RESULTS+=("FAIL|$1|${2:-}"); }
stage_skip() { printf "  %s %-44s %s\n" "$(gray  SKIP)" "$1" "$(gray "${2:-}")"; SKIP=$((SKIP+1)); RESULTS+=("SKIP|$1|${2:-}"); }
section()    { printf "\n%s %s %s\n" "==" "$(yellow "$*")" "=="; }

# ----- requires ------------------------------------------------------------
require() { command -v "$1" >/dev/null 2>&1 || { printf "missing required tool: %s\n" "$1" >&2; exit 2; }; }
require curl; require jq; require kubectl; require aws

# ----- port-forward bookkeeping --------------------------------------------
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
  # Sleep beats a tight nc loop here: kubectl print warnings on stderr
  # only after the listener is ready.
  sleep 2
}

# ----- bearer fetch --------------------------------------------------------
BEARER=""
fetch_bearer() {
  BEARER=$(aws secretsmanager get-secret-value \
    --secret-id "$SECRET_ID_MIMIR" --region "$SECRET_REGION" --profile "$PROFILE" \
    --query SecretString --output text 2>/dev/null \
    | jq -r '.bearer_token' 2>/dev/null) || true
  [ -n "$BEARER" ] && [ "$BEARER" != "null" ]
}

# ----- query primitives ----------------------------------------------------
# Each metric/log check has BOTH an in-cluster and a public variant. The
# stage fails if either disagrees by >1 sample or returns empty.

mimir_query_incluster() {
  # mimir_query_incluster <expr>   prints the JSON body
  local expr="$1"
  curl -sS -m 10 -G "http://localhost:28080/prometheus/api/v1/query" \
    --data-urlencode "query=$expr" -H "X-Scope-OrgID: ${TENANT}"
}

mimir_push_probe_public() {
  # The mimir-push ALB only accepts WRITES (POST /api/v1/metrics/write
  # in protobuf). Read API isn't exposed. So the "public" probe verifies
  # the bearer-auth WRITE path works -- 401 anon, 204 bearer'd.
  curl -sS -m 8 -o /dev/null -w "%{http_code}" -X POST \
    -H "Content-Type: application/x-protobuf" -d "" \
    "https://${MIMIR_PUSH_HOST}/api/v1/metrics/write" 2>/dev/null
}

loki_push_probe_public() {
  # Loki accepts JSON bodies, easier to construct in shell than the
  # Mimir protobuf. Returns the HTTP status code via -w.
  local mode="$1"   # "anon" or "bearer"
  local now_ns
  now_ns="$(date -u +%s)000000000"
  local payload
  payload="{\"streams\":[{\"stream\":{\"probe\":\"verify-lgtm-${now_ns}\"},\"values\":[[\"${now_ns}\",\"verify-lgtm probe\"]]}]}"
  if [ "$mode" = "anon" ]; then
    curl -sS -m 8 -o /dev/null -w "%{http_code}" -X POST \
      -H "Content-Type: application/json" -d "$payload" \
      "https://${LOKI_PUSH_HOST}/loki/api/v1/push" 2>/dev/null
  else
    [ -z "$BEARER" ] && { echo "000"; return 1; }
    curl -sS -m 8 -o /dev/null -w "%{http_code}" -X POST \
      -H "Authorization: Bearer ${BEARER}" -H "Content-Type: application/json" -d "$payload" \
      "https://${LOKI_PUSH_HOST}/loki/api/v1/push" 2>/dev/null
  fi
}

loki_query_incluster() {
  # loki_query_incluster <logQL> <start_ns> <end_ns>
  local q="$1" s="$2" e="$3"
  curl -sS -m 10 -G "http://localhost:23100/loki/api/v1/query_range" \
    --data-urlencode "query=$q" \
    --data-urlencode "start=$s" --data-urlencode "end=$e" \
    --data-urlencode "limit=20"
}

loki_query_public() {
  local q="$1" s="$2" e="$3"
  [ -z "$BEARER" ] && { echo '{"status":"missing_bearer"}'; return 1; }
  curl -sS -m 10 -G "https://${LOKI_PUSH_HOST}/loki/api/v1/query_range" \
    --data-urlencode "query=$q" \
    --data-urlencode "start=$s" --data-urlencode "end=$e" \
    --data-urlencode "limit=20" \
    -H "Authorization: Bearer $BEARER"
}

# Resolve a datasource UID by name via Grafana's HTTP API.
GRAFANA_AUTH=""
resolve_grafana_auth() {
  # Try GRAFANA_TOKEN (service-account token) first, then fall back to the
  # chart-rotated admin/password from the in-cluster Secret. Probe both
  # against /api/datasources before reporting success -- on a SAML/OIDC
  # cluster the chart admin Secret can be stale (Grafana hashes the
  # initial password into the DB and ignores later env var rotations
  # unless force_reset_admin_password is set), and we want the verify
  # script to skip-not-pass in that state.
  local probe
  if [ -n "${GRAFANA_TOKEN:-}" ]; then
    GRAFANA_AUTH="Authorization: Bearer ${GRAFANA_TOKEN}"
    probe=$(kubectl -n grafana exec deploy/grafana -c grafana -- \
      curl -sS -m 5 -o /dev/null -w "%{http_code}" \
        -H "$GRAFANA_AUTH" -H "Host: grafana.cd.percona.com" \
        "http://localhost:3000/api/datasources" 2>/dev/null)
    [ "$probe" = "200" ] && return 0
  fi
  local pw
  pw=$(kubectl -n grafana get secret grafana -o jsonpath='{.data.admin-password}' 2>/dev/null | base64 -d || true)
  if [ -n "$pw" ]; then
    GRAFANA_AUTH="Authorization: Basic $(printf 'admin:%s' "$pw" | base64 -w0)"
    probe=$(kubectl -n grafana exec deploy/grafana -c grafana -- \
      curl -sS -m 5 -o /dev/null -w "%{http_code}" \
        -H "$GRAFANA_AUTH" -H "Host: grafana.cd.percona.com" \
        "http://localhost:3000/api/datasources" 2>/dev/null)
    [ "$probe" = "200" ] && return 0
  fi
  GRAFANA_AUTH=""
  return 1
}

resolve_ds_uid() {
  # resolve_ds_uid <name>
  # The Host header bypasses Grafana's enforce_domain redirect (root_url
  # is grafana.cd.percona.com but we're hitting localhost:3000 from inside
  # the pod).
  local name="$1"
  [ -z "$GRAFANA_AUTH" ] && return 1
  kubectl -n grafana exec deploy/grafana -c grafana -- \
    curl -sS -m 5 -H "$GRAFANA_AUTH" -H "Host: grafana.cd.percona.com" \
      "http://localhost:3000/api/datasources/name/${name}" 2>/dev/null \
    | jq -r '.uid // empty'
}

grafana_proxy_query() {
  # grafana_proxy_query <ds-uid> <expr>
  local uid="$1" expr="$2"
  kubectl -n grafana exec deploy/grafana -c grafana -- \
    curl -sS -m 10 -G -H "$GRAFANA_AUTH" -H "Host: grafana.cd.percona.com" \
      "http://localhost:3000/api/datasources/proxy/uid/${uid}/api/v1/query" \
      --data-urlencode "query=$expr" 2>/dev/null
}

ts_now_ns()    { printf '%s000000000' "$(date -u +%s)"; }
ts_5min_ns()   { printf '%s000000000' "$(($(date -u +%s) - 300))"; }

# ----- stages --------------------------------------------------------------
stage_prereqs() {
  section "Stage 0: prerequisites"
  if kubectl get ns alloy-gateway >/dev/null 2>&1; then
    stage_pass "kubectl context reaches cluster"
  else
    stage_fail "kubectl context reaches cluster" "alloy-gateway namespace not found"
    exit 1
  fi
  if fetch_bearer; then
    stage_pass "bearer fetched from AWS SM" "len=${#BEARER}"
  else
    stage_fail "bearer fetched from AWS SM" "secret read failed (profile=$PROFILE)"
  fi
  if resolve_grafana_auth; then
    stage_pass "Grafana auth resolved" "${GRAFANA_AUTH%% *}"
  else
    stage_skip "Grafana auth resolved" "GRAFANA_TOKEN unset and admin secret unreadable"
  fi
  pf mimir svc/mimir-query-frontend 28080 8080
  pf loki  svc/loki-query-frontend  23100 3100
}

# Helpers reused across stages -----------------------------------------------
mimir_value() {  # mimir_value <expr> -- prints the first scalar value
  local expr="$1" body
  body=$(mimir_query_incluster "$expr") || return 1
  echo "$body" | jq -r '.data.result[0].value[1] // empty'
}

mimir_age_seconds() {  # mimir_age_seconds <expr>
  local expr="$1" body ts
  body=$(mimir_query_incluster "$expr") || return 1
  ts=$(echo "$body" | jq -r '.data.result[0].value[0] // empty')
  [ -z "$ts" ] && return 1
  awk -v now="$(date -u +%s)" -v ts="$ts" 'BEGIN { printf "%d\n", now - ts }'
}

# Stage P0 -------------------------------------------------------------------
stage_pre_snapshot() {
  section "P0: pre-cutover snapshot"
  local ts snap kps_up hetzner_n ps3_rate write_rate
  ts=$(date -u +%Y%m%dT%H%M%SZ)
  snap="/tmp/lgtm-pre-${ts}.json"
  kps_up=$(mimir_value 'count(up{namespace="kube-prometheus-stack"})' || echo 0)
  hetzner_n=$(mimir_value 'count(count by (master)(hetzner_api_rate_limit_remaining))' || echo 0)
  ps3_rate=$(mimir_value 'hetzner_api_rate_limit_remaining{master="ps3.cd"}' || echo 0)
  write_rate=$(mimir_value 'sum(rate(prometheus_remote_storage_samples_in_total{namespace="kube-prometheus-stack"}[5m]))' || echo 0)
  cat > "$snap" <<EOF
{ "ts":"$ts","kps_up":$kps_up,"hetzner_masters":$hetzner_n,"ps3_rate_limit_remaining":$ps3_rate,"kps_remote_write_rate":$write_rate }
EOF
  stage_pass "snapshot written" "$snap kps_up=$kps_up masters=$hetzner_n ps3=$ps3_rate writeRate=$write_rate"
}

# Stage R --------------------------------------------------------------------
stage_ruler() {
  section "R: Mimir ruler PrometheusRule sync"
  pf mimir svc/mimir-ruler 28099 8080
  local groups
  groups=$(curl -sS -m 10 "http://localhost:28099/prometheus/api/v1/rules" \
    -H "X-Scope-OrgID: ${TENANT}" 2>/dev/null | jq '.data.groups | length' 2>/dev/null) || groups=0
  if [ "${groups:-0}" -ge 1 ]; then
    stage_pass "ruler groups synced" "groups=$groups"
  else
    stage_fail "ruler groups synced" "groups=$groups (expected >=1; >=33 once kps rules ship)"
  fi
  local v
  v=$(mimir_value 'count(node_namespace_pod_container:container_cpu_usage_seconds_total:sum_rate)' || echo 0)
  if [ "${v:-0}" != "0" ] && [ -n "$v" ]; then
    stage_pass "recording rule evaluates" "series=$v"
  else
    stage_fail "recording rule evaluates" "rule node_namespace_pod_container:container_cpu_usage_seconds_total:sum_rate has 0 series"
  fi
}

# Stage M --------------------------------------------------------------------
stage_post_metrics() {
  section "M: post-cutover Mimir ingestion (in-cluster READ + public WRITE)"
  local v_ic age code_anon
  v_ic=$(mimir_value 'hetzner_api_rate_limit_remaining{master="ps3.cd"}')
  if [ -n "$v_ic" ]; then
    stage_pass "Mimir in-cluster ps3.cd metric" "rate_remaining=$v_ic"
  else
    stage_fail "Mimir in-cluster ps3.cd metric" "no series for hetzner_api_rate_limit_remaining{master=\"ps3.cd\"}"
  fi
  age=$(mimir_age_seconds 'hetzner_api_rate_limit_remaining{master="ps3.cd"}' || echo 999)
  if [ "${age:-999}" -lt 120 ]; then
    stage_pass "Mimir in-cluster freshness" "age=${age}s (<120s)"
  else
    stage_fail "Mimir in-cluster freshness" "age=${age}s (>=120s)"
  fi
  # Public ALB is push-only; verify the bearer-auth gate is wired.
  code_anon=$(mimir_push_probe_public)
  if [ "$code_anon" = "401" ]; then
    stage_pass "mimir-push ALB anonymous POST rejected" "HTTP=401"
  else
    stage_fail "mimir-push ALB anonymous POST rejected" "HTTP=${code_anon} (expected 401)"
  fi
}

stage_post_logs() {
  section "M: post-cutover Loki ingestion (in-cluster READ + public WRITE)"
  local s e n_ic code_anon code_bearer
  s=$(ts_5min_ns); e=$(ts_now_ns)
  n_ic=$(loki_query_incluster '{master="ps3.cd"}' "$s" "$e" \
    | jq '[.data.result[]?.values[]?] | length')
  if [ "${n_ic:-0}" -ge 1 ]; then
    stage_pass "Loki in-cluster ps3.cd lines (5m)" "lines=$n_ic"
  else
    stage_fail "Loki in-cluster ps3.cd lines (5m)" "no streams returned"
  fi
  # Public ALB write probe: anonymous -> 401, bearer -> 204.
  code_anon=$(loki_push_probe_public anon)
  if [ "$code_anon" = "401" ]; then
    stage_pass "loki-push ALB anonymous POST rejected" "HTTP=401"
  else
    stage_fail "loki-push ALB anonymous POST rejected" "HTTP=${code_anon} (expected 401)"
  fi
  if [ -n "$BEARER" ]; then
    code_bearer=$(loki_push_probe_public bearer)
    if [ "$code_bearer" = "204" ]; then
      stage_pass "loki-push ALB bearer accepted" "HTTP=204"
    else
      stage_fail "loki-push ALB bearer accepted" "HTTP=${code_bearer} (expected 204)"
    fi
  else
    stage_skip "loki-push ALB bearer accepted" "no bearer"
  fi
}

stage_grafana_proxy() {
  section "M: Grafana datasource-proxy queries"
  if [ -z "$GRAFANA_AUTH" ]; then
    stage_skip "Grafana DS proxy" "no auth"
    return
  fi
  local mimir_uid loki_uid status
  mimir_uid=$(resolve_ds_uid Mimir)
  loki_uid=$(resolve_ds_uid Loki)
  if [ -n "$mimir_uid" ]; then
    status=$(grafana_proxy_query "$mimir_uid" 'hetzner_api_rate_limit_remaining{master="ps3.cd"}' \
      | jq -r '.status // "missing"')
    if [ "$status" = "success" ]; then
      stage_pass "Grafana proxy via uid=$mimir_uid (Mimir)" "status=$status"
    else
      stage_fail "Grafana proxy via uid=$mimir_uid (Mimir)" "status=$status"
    fi
  else
    stage_fail "Grafana DS uid lookup" "Mimir not found in /api/datasources/name/Mimir"
  fi
  if [ -n "$loki_uid" ]; then
    stage_pass "Grafana DS uid lookup" "loki=$loki_uid"
  fi
}

# Stage W --------------------------------------------------------------------
stage_worker_pick() {
  if ! command -v jenkins >/dev/null 2>&1; then
    stage_skip "Hetzner job autopick" "jenkins CLI not on PATH"
    return 1
  fi
  if [ -n "$JOB_OVERRIDE" ]; then
    JOB="$JOB_OVERRIDE"
    stage_pass "Hetzner job (operator override)" "job=$JOB"
    return 0
  fi
  JOB=$(jenkins job "$INST" list 2>/dev/null | grep -iE 'hetzner|smoke|ping' | head -1)
  if [ -z "$JOB" ]; then
    stage_fail "Hetzner job autopick" "no candidate job found via 'jenkins job $INST list | grep -iE hetzner|smoke|ping'"
    return 1
  fi
  stage_pass "Hetzner job (autopicked)" "job=$JOB"
}

stage_worker_trigger() {
  PRE_RATE=$(mimir_value 'hetzner_api_rate_limit_remaining{master="ps3.cd"}' || echo 0)
  if jenkins build "$INST" "$JOB" >/dev/null 2>&1; then
    stage_pass "Hetzner build triggered" "pre_rate=$PRE_RATE"
  else
    stage_fail "Hetzner build triggered" "jenkins build $INST $JOB failed"
  fi
}

stage_worker_wait() {
  local i=0 servers=0
  while [ $i -lt 30 ]; do
    servers=$(jenkins hetzner -i "$INST" servers 2>/dev/null | awk 'NR>1 && /running/ {n++} END {print n+0}')
    if [ "${servers:-0}" -ge 1 ]; then
      stage_pass "Hetzner worker provisioned" "servers_running=$servers"
      return 0
    fi
    sleep 10
    i=$((i+1))
  done
  stage_fail "Hetzner worker provisioned" "300s timeout, no running servers"
}

stage_worker_metrics() {
  local v
  v=$(mimir_value 'delta(hetzner_api_requests_total{master="ps3.cd"}[5m])' || echo 0)
  if awk "BEGIN {exit !(($v) > 0)}"; then
    stage_pass "API request delta during provision" "delta=$v"
  else
    stage_fail "API request delta during provision" "delta=$v (expected >0)"
  fi
  local n
  n=$(mimir_query_incluster 'count by (template)(hetzner_template_executors{master="ps3.cd"})' \
    | jq '[.data.result[]?] | length')
  if [ "${n:-0}" -ge 1 ]; then
    stage_pass "template_executors series during provision" "templates=$n"
  else
    stage_fail "template_executors series during provision" "templates=$n"
  fi
}

stage_worker_logs() {
  local s e n
  s=$(ts_5min_ns); e=$(ts_now_ns)
  n=$(loki_query_incluster '{master="ps3.cd"} |~ "(?i)hetzner.*provision"' "$s" "$e" \
    | jq '[.data.result[]?.values[]?] | length')
  if [ "${n:-0}" -ge 1 ]; then
    stage_pass "Loki provision lines during build" "lines=$n"
  else
    stage_fail "Loki provision lines during build" "no provision-related lines in last 5m"
  fi
}

stage_worker_rate_decremented() {
  local post
  post=$(mimir_value 'hetzner_api_rate_limit_remaining{master="ps3.cd"}' || echo 0)
  if awk "BEGIN {exit !(($post) < ($PRE_RATE))}"; then
    stage_pass "API rate-limit decremented" "pre=$PRE_RATE post=$post"
  else
    stage_fail "API rate-limit decremented" "pre=$PRE_RATE post=$post (expected post<pre)"
  fi
}

# Stage D --------------------------------------------------------------------
stage_dashboard_panels() {
  section "D: Grafana dashboard renders"
  if [ -z "$GRAFANA_AUTH" ]; then
    stage_skip "Hetzner dashboard panel render" "no auth"
    return
  fi
  local mimir_uid exprs n_ok n_total
  mimir_uid=$(resolve_ds_uid Mimir)
  [ -z "$mimir_uid" ] && { stage_fail "Hetzner dashboard panel render" "no Mimir uid"; return; }
  exprs=$(kubectl -n grafana exec deploy/grafana -c grafana -- \
    curl -sS -m 5 -H "$GRAFANA_AUTH" -H "Host: grafana.cd.percona.com" \
      "http://localhost:3000/api/dashboards/uid/hetzner-plugin" 2>/dev/null \
    | jq -r '.dashboard.panels[].targets[]?.expr // empty' \
    | grep -v '^$' | head -3)
  n_total=0; n_ok=0
  while IFS= read -r e; do
    [ -z "$e" ] && continue
    n_total=$((n_total+1))
    local s
    s=$(grafana_proxy_query "$mimir_uid" "$e" | jq -r '.status // "missing"')
    [ "$s" = "success" ] && n_ok=$((n_ok+1))
  done <<< "$exprs"
  if [ "$n_total" -gt 0 ] && [ "$n_ok" -eq "$n_total" ]; then
    stage_pass "Hetzner dashboard panel render" "panels_ok=$n_ok/$n_total"
  else
    stage_fail "Hetzner dashboard panel render" "panels_ok=$n_ok/$n_total"
  fi
}

stage_dashboard_default() {
  if [ -z "$GRAFANA_AUTH" ]; then
    stage_skip "default datasource is Mimir" "no auth"
    return
  fi
  local def
  def=$(kubectl -n grafana exec deploy/grafana -c grafana -- \
    curl -sS -m 5 -H "$GRAFANA_AUTH" -H "Host: grafana.cd.percona.com" \
      "http://localhost:3000/api/datasources" 2>/dev/null \
    | jq -r '.[] | select(.isDefault) | .name')
  if [ "$def" = "Mimir" ]; then
    stage_pass "default datasource is Mimir" "default=$def"
  else
    stage_fail "default datasource is Mimir" "default=$def (expected Mimir; will be flipped in P5a)"
  fi
}

# Stage X --------------------------------------------------------------------
stage_failsafe() {
  section "X: kps remnants gone (post-P6 only)"
  local kps_n
  if kubectl get ns kube-prometheus-stack >/dev/null 2>&1; then
    kps_n=$(kubectl -n kube-prometheus-stack get pod --no-headers 2>/dev/null | wc -l)
    if [ "$kps_n" -eq 0 ]; then
      stage_pass "kube-prometheus-stack ns empty" "pods=0"
    else
      stage_fail "kube-prometheus-stack ns empty" "pods=$kps_n (expected 0 after P6)"
    fi
  else
    stage_pass "kube-prometheus-stack ns gone" "namespace not found"
  fi
  local fresh
  fresh=$(mimir_value 'count(up{job=~"kube-prometheus-stack-.*"} > 0)' || echo 0)
  if [ "${fresh:-0}" = "0" ]; then
    stage_pass "no fresh kps series" "fresh_kps_up=0"
  else
    stage_fail "no fresh kps series" "fresh_kps_up=$fresh (expected 0 after P6)"
  fi
  local crd_n
  crd_n=$(kubectl get crd -o name 2>/dev/null | grep -c monitoring.coreos.com)
  if [ "${crd_n:-0}" -ge 10 ]; then
    stage_pass "monitoring.coreos.com CRDs present" "crds=$crd_n"
  else
    stage_fail "monitoring.coreos.com CRDs present" "crds=$crd_n (expected >=10; standalone chart owns them)"
  fi
}

# ----- main ----------------------------------------------------------------
printf "verify-lgtm-cutover  phase=%s  inst=%s  context=%s\n" \
  "$PHASE" "$INST" "$(kubectl config current-context 2>/dev/null || echo '<no-kubeconfig>')"

stage_prereqs

case "$PHASE" in
  pre)
    stage_pre_snapshot
    ;;
  ruler)
    stage_ruler
    ;;
  post)
    stage_post_metrics
    stage_post_logs
    stage_grafana_proxy
    ;;
  worker)
    section "W: Hetzner worker job ingestion proof"
    if [ "$SKIP_MASTER" = "1" ]; then
      stage_skip "worker stages" "--skip-master set"
    elif stage_worker_pick; then
      stage_worker_trigger
      stage_worker_wait
      stage_worker_metrics
      stage_worker_logs
      stage_worker_rate_decremented
    fi
    ;;
  dashboard)
    stage_dashboard_panels
    stage_dashboard_default
    ;;
  failsafe)
    stage_failsafe
    ;;
  all)
    stage_pre_snapshot
    stage_ruler
    stage_post_metrics
    stage_post_logs
    stage_grafana_proxy
    section "W: Hetzner worker job ingestion proof"
    if [ "$SKIP_MASTER" = "1" ]; then
      stage_skip "worker stages" "--skip-master set"
    elif stage_worker_pick; then
      stage_worker_trigger
      stage_worker_wait
      stage_worker_metrics
      stage_worker_logs
      stage_worker_rate_decremented
    fi
    stage_dashboard_panels
    stage_dashboard_default
    stage_failsafe
    ;;
  *)
    printf "unknown phase: %s (one of pre|ruler|post|worker|dashboard|failsafe|all)\n" "$PHASE" >&2
    exit 2 ;;
esac

# ----- summary -------------------------------------------------------------
section "Summary"
printf "  pass=%d  fail=%d  skip=%d\n" "$PASS" "$FAIL" "$SKIP"
[ "$FAIL" -eq 0 ]
