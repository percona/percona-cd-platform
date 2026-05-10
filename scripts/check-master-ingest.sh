#!/usr/bin/env bash
# check-master-ingest.sh - per-master Mimir + Loki ingest probe.
#
# Reads from the in-cluster query-frontends (no Grafana auth needed) to verify
# every Jenkins master is currently shipping metrics + logs through alloy-gateway.
# Output is one row per master: Mimir series count, Mimir freshness (seconds since
# last sample), Mimir distinct metric-name cardinality, Loki log-line count over
# the lookback window. Quiet masters can show 0 in narrow windows; widen with
# `--lookback 24h` to cross-check.
#
# Usage:
#   scripts/check-master-ingest.sh                       # default 1h window, table
#   scripts/check-master-ingest.sh --lookback 24h        # 24h window
#   scripts/check-master-ingest.sh --json                # JSON output
#   scripts/check-master-ingest.sh --csv                 # CSV output
#
# Read-only. Exits 0 if every expected master is present in Mimir, nonzero if
# any master is missing (Loki gaps are reported but don't fail; quiet masters
# legitimately have 0 lines in narrow windows).
#
# Requires on PATH: kubectl, curl, jq.
# Requires kubeconfig with context `percona-ci-platform`.

set -u
set -o pipefail

LOOKBACK="1h"
FORMAT="table"
TENANT="percona-ci"
EXPECTED_MASTERS=(cloud.cd pg.cd pmm.cd ps3.cd ps57.cd ps80.cd psmdb.cd pxb.cd pxc.cd rel.cd)

# Pick local ports unlikely to clash with anything else.
MIMIR_PORT=58080
LOKI_PORT=53100

while [[ $# -gt 0 ]]; do
  case "$1" in
    --lookback) LOOKBACK="$2"; shift 2 ;;
    --lookback=*) LOOKBACK="${1#*=}"; shift ;;
    --json) FORMAT="json"; shift ;;
    --csv) FORMAT="csv"; shift ;;
    --table) FORMAT="table"; shift ;;
    -h|--help)
      sed -n '2,21p' "$0" | sed 's/^# \{0,1\}//'
      exit 0
      ;;
    *) printf 'unknown arg: %s\n' "$1" >&2; exit 2 ;;
  esac
done

MIMIR_PF=""
LOKI_PF=""
cleanup() {
  [[ -n "$MIMIR_PF" ]] && kill "$MIMIR_PF" 2>/dev/null || true
  [[ -n "$LOKI_PF" ]] && kill "$LOKI_PF" 2>/dev/null || true
  wait 2>/dev/null || true
}
trap cleanup EXIT

kubectl --context percona-ci-platform -n mimir port-forward svc/mimir-query-frontend "$MIMIR_PORT:8080" >/dev/null 2>&1 &
MIMIR_PF=$!
kubectl --context percona-ci-platform -n loki port-forward svc/loki-query-frontend "$LOKI_PORT:3100" >/dev/null 2>&1 &
LOKI_PF=$!

# Wait for both ports to accept connections.
for port in $MIMIR_PORT $LOKI_PORT; do
  for _ in $(seq 1 30); do
    (echo > "/dev/tcp/127.0.0.1/$port") >/dev/null 2>&1 && break
    sleep 0.3
  done
done

mimir() {
  curl -sG --data-urlencode "query=$1" \
    -H "X-Scope-OrgID: $TENANT" \
    "http://127.0.0.1:$MIMIR_PORT/prometheus/api/v1/query"
}

loki() {
  curl -sG --data-urlencode "query=$1" \
    -H "X-Scope-OrgID: $TENANT" \
    "http://127.0.0.1:$LOKI_PORT/loki/api/v1/query"
}

# 1. Series count per master from a known-emitted gauge (every master pushes this).
SERIES_JSON=$(mimir 'count by (master) (hetzner_api_rate_limit_remaining)')
# 2. Seconds since last sample per master.
FRESH_JSON=$(mimir 'time() - max by (master) (timestamp(hetzner_api_rate_limit_remaining))')
# 3. Distinct metric-name cardinality per master (event-driven counters reflect here).
CARD_JSON=$(mimir 'count by (master) (group by (master, __name__) ({master=~".+"}))')
# 4. Loki log-line count per master over the lookback window.
LOKI_JSON=$(loki "sum by (master) (count_over_time({fleet=\"percona-jenkins\"}[${LOOKBACK}]))")

# Build a flat per-master record.
RECORDS=$(jq -n \
  --argjson s "$SERIES_JSON" \
  --argjson f "$FRESH_JSON" \
  --argjson c "$CARD_JSON" \
  --argjson l "$LOKI_JSON" \
  --argjson expected "$(printf '%s\n' "${EXPECTED_MASTERS[@]}" | jq -R . | jq -s .)" '
  def lookup($v):
    ($v.data.result // []) | map({(.metric.master): .value[1]}) | add // {};
  ($expected | map({master: .})) as $base
  | (lookup($s)) as $sm
  | (lookup($f)) as $fm
  | (lookup($c)) as $cm
  | (lookup($l)) as $lm
  | $base | map(. + {
      mimir_series:  ($sm[.master] // null),
      mimir_freshness_s: ($fm[.master] // null),
      mimir_cardinality: ($cm[.master] // null),
      loki_lines:    ($lm[.master] // null)
    })')

case "$FORMAT" in
  json)
    echo "$RECORDS" | jq .
    ;;
  csv)
    echo "master,mimir_series,mimir_freshness_s,mimir_cardinality,loki_lines"
    echo "$RECORDS" | jq -r '.[] | [.master, .mimir_series, .mimir_freshness_s, .mimir_cardinality, .loki_lines] | @csv'
    ;;
  table)
    printf 'lookback=%s tenant=%s\n\n' "$LOOKBACK" "$TENANT"
    {
      printf 'MASTER\tMIMIR_SERIES\tFRESHNESS_S\tCARDINALITY\tLOKI_LINES\n'
      echo "$RECORDS" | jq -r '.[]
        | [.master,
           (.mimir_series // "MISSING"),
           ((.mimir_freshness_s // "-") | (if (type == "string" and . == "-") then "-" else (tonumber | floor | tostring) end)),
           (.mimir_cardinality // "-"),
           (.loki_lines // "-")
          ] | @tsv'
    } | column -t -s $'\t'
    ;;
esac

# Exit nonzero if any expected master is missing from Mimir.
MISSING=$(echo "$RECORDS" | jq -r '[.[] | select(.mimir_series == null) | .master] | join(",")')
if [[ -n "$MISSING" ]]; then
  printf '\nMISSING from Mimir: %s\n' "$MISSING" >&2
  exit 1
fi
exit 0
