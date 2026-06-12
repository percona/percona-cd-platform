#!/usr/bin/env bash
# CI gate: render the per-instance Jenkins controller chart and assert the values
# actually reach the upstream `jenkins` subchart. Regression guard for the
# top-level-vs-`jenkins:`-scoped bug that silently left ps3-k8s on chart defaults
# (resources/jenkins/master has no real templates/, so top-level `controller:`
# values went nowhere; they MUST be nested under the `jenkins:` dependency key).
#
# Renders values-base.yaml alone, then base + each instances/<host> and each
# _disabled/<host> overlay, and asserts the controller actually came up as OUR
# image on the dedicated node pool with the Retain PVC + ClusterIP agent Service.
#
# Run from the repo root:  scripts/jenkins-chart-render-check.sh
set -euo pipefail

CHART="resources/jenkins/master"
[ -f "$CHART/Chart.yaml" ] || { echo "run from repo root (no $CHART/Chart.yaml)"; exit 2; }

( cd "$CHART" && { helm dependency build >/dev/null 2>&1 || helm dependency update >/dev/null; } )

fail=0
assert() { if ! grep -q "$2" <<<"$3"; then echo "  FAIL [$1]: missing '$2'"; fail=1; fi; }
refute() { if   grep -q "$2" <<<"$3"; then echo "  FAIL [$1]: unexpected '$2'"; fail=1; fi; }

render_check() {
  local name="$1"; shift
  local out
  if ! out="$(helm template "$name" "$CHART" "$@" 2>&1)"; then
    echo "  FAIL [$name]: helm template errored"; echo "$out" | tail -8; fail=1; return
  fi
  assert "$name" 'percona-cd/jenkins-percona'                "$out"  # our controller image reached the subchart
  assert "$name" 'kind: StatefulSet'                         "$out"
  # Persistence reaches the subchart as EITHER a templated AZ-pinned Retain PVC
  # (gp3-jenkins-1a-retain) OR a pre-bound restored-home existingClaim (the chart
  # then templates no PVC, so the storageClass string is absent by design).
  if ! grep -qE 'gp3-jenkins-1a-retain|claimName: .+-jenkins-home' <<<"$out"; then
    echo "  FAIL [$name]: persistence not wired (no Retain SC and no *-jenkins-home existingClaim)"; fail=1
  fi
  assert "$name" 'group.name: jenkins-masters'               "$out"  # shared ALB group
  assert "$name" 'name: agent-listener'                      "$out"  # agent Service port wiring (listener dark; clouds connect via SSH)
  refute "$name" 'type: LoadBalancer'                       "$out"  # no internet-facing agent NLB regression
  assert "$name" 'workload.percona.com/tier: jenkins-master' "$out"  # pinned to the dedicated node pool
  refute "$name" 'image: "jenkins/jenkins'                   "$out"  # upstream-default controller image must NOT win
  if [ "$fail" -eq 0 ]; then echo "  ok [$name]"; fi
}

echo "jenkins chart render-check:"
render_check base -f "$CHART/values-base.yaml"
for inst in "$CHART"/instances/*/ resources/jenkins/_disabled/*/; do
  [ -f "${inst}values.yaml" ] || continue
  render_check "$(basename "$inst")" -f "$CHART/values-base.yaml" -f "${inst}values.yaml"
done

if [ "$fail" -eq 0 ]; then echo "PASS"; else echo "FAIL"; exit 1; fi
