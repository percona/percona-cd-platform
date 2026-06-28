#!/usr/bin/env bash
# check-arch-readiness.sh - arch-migration preflight gate.
#
# Before flipping a NodePool or managed node group to a new CPU arch (e.g. the
# amd64 -> arm64/Graviton migration), this asserts every workload that could
# land on a cluster node is ready for the target arch. It catches the two ways a
# pod strands `Pending` after the last old-arch node is gone:
#
#   Prong A - image arch coverage: every image referenced by a
#     Deployment / StatefulSet / DaemonSet / CronJob / Job publishes a
#     `linux/<target>` manifest. A single-arch (e.g. amd64-only) image cannot
#     run on the new nodes. (This is the snapscheduler failure: its upstream
#     image ships amd64-only.)
#   Prong B - arch-pin scan: no PodSpec pins `kubernetes.io/arch` (nodeSelector
#     or nodeAffinity) to an arch other than the target. Even a multi-arch image
#     strands behind a wrong-arch pin. (snapscheduler also hard-pinned amd64.)
#
# Read-only. Exits 0 only if BOTH prongs pass for every workload. Run it as the
# preflight before the cutover, not after.
#
# Usage:
#   scripts/check-arch-readiness.sh                 # target arm64 (default)
#   scripts/check-arch-readiness.sh amd64           # target a different arch
#   AWS_PROFILE=percona-dev-admin scripts/check-arch-readiness.sh
#
# Requires: kubectl (current-context cluster), jq, and docker buildx (to inspect
# image manifests; ECR images are auto-logged-in via AWS_PROFILE).

set -euo pipefail

target_arch="${1:-arm64}"

err() {
  echo "$*" >&2
}

for tool in kubectl jq docker aws; do
  command -v "${tool}" >/dev/null || {
    err "missing required tool: ${tool}"
    exit 1
  }
done

# ---- collect every image and every arch nodeSelector/affinity from PodSpecs ----
# CronJobs nest the PodSpec one level deeper (.spec.jobTemplate...); everything
# else uses .spec.template.spec. The jq below normalizes both and emits, per
# workload: the images, and any kubernetes.io/arch values it pins.
workloads_json="$(kubectl get deploy,statefulset,daemonset,cronjob,job -A -o json)"

# Capture each pipeline into a variable with an EXPLICIT exit check, THEN mapfile.
# `mapfile < <(... | jq)` runs the pipeline in a subshell, so a jq/kubectl
# failure does NOT trip `set -e` and would leave an empty array -> a false-green.
images_raw="$(echo "${workloads_json}" | jq -r '
    .items[]
    | (.spec.template.spec // .spec.jobTemplate.spec.template.spec) as $ps
    | ($ps.containers // []) + ($ps.initContainers // [])
    | .[].image
  ' | sort -u)" || { err "failed to extract images (jq/kubectl error)"; exit 1; }
mapfile -t images < <(printf '%s\n' "${images_raw}" | grep -v '^$' || true)
if ((${#images[@]} == 0)); then
  err "no workload images found (wrong kube-context, or empty cluster?)"
  exit 1
fi

# Wrong-arch pins, with correct selector semantics. A workload EXCLUDES the
# target arch when: its nodeSelector arch differs; OR its required node-affinity
# excludes it, i.e. EVERY ORed nodeSelectorTerm has an arch matchExpression that
# excludes the target. `operator` is honoured (In/NotIn/Exists/DoesNotExist):
# `In [amd64, arm64]` permits arm64, `NotIn [arm64]` excludes it.
bad_pins_raw="$(echo "${workloads_json}" | jq -r --arg target "${target_arch}" '
    def expr_excludes($e; $t):
      ($e.operator // "In") as $op | ($e.values // []) as $vals
      | if   $op == "In"           then ($vals | index($t) | not)
        elif $op == "NotIn"        then ($vals | index($t) | . != null)
        elif $op == "DoesNotExist" then true
        elif $op == "Exists"       then false
        else false end;
    def term_excludes($t):
      (.matchExpressions // []) | any(.[]; .key == "kubernetes.io/arch" and expr_excludes(.; $t));
    .items[]
    | { ns: .metadata.namespace, kind: .kind, name: .metadata.name,
        ps: (.spec.template.spec // .spec.jobTemplate.spec.template.spec) } as $w
    | ($w.ps.nodeSelector["kubernetes.io/arch"]) as $sel
    | ($w.ps.affinity.nodeAffinity.requiredDuringSchedulingIgnoredDuringExecution.nodeSelectorTerms) as $terms
    | select(
        ($sel != null and $sel != $target)
        or ( ($terms | type) == "array" and ($terms | length) > 0
             and ($terms | all(.[]; term_excludes($target))) )
      )
    | "\($w.ns)/\($w.kind)/\($w.name) excludes kubernetes.io/arch=\($target)"
  ')" || { err "failed to evaluate arch pins (jq error)"; exit 1; }
mapfile -t bad_pins < <(printf '%s\n' "${bad_pins_raw}" | grep -v '^$' || true)

# ---- log into every registry the images reference ----
# Each distinct ECR host (ours AND AWS-owned accounts like the EKS-addon
# registry 602401143452, which grants cross-account pull) authenticates with our
# own token; plus ECR Public for public.ecr.aws. Without this, those images fail
# to inspect and false-fail Prong A.
ecr_hosts="$(printf '%s\n' "${images[@]}" | grep -oE '[0-9]+\.dkr\.ecr\.[a-z0-9-]+\.amazonaws\.com' | sort -u || true)"
if [[ -n "${ecr_hosts}" ]]; then
  : "${AWS_PROFILE:?AWS_PROFILE must be set to inspect ECR images}"
  while IFS= read -r host; do
    [[ -z "${host}" ]] && continue
    host_region="$(cut -d. -f4 <<<"${host}")"
    aws ecr get-login-password --region "${host_region}" 2>/dev/null \
      | docker login --username AWS --password-stdin "${host}" >/dev/null 2>&1 \
      || err "warning: could not log into ${host}"
  done <<<"${ecr_hosts}"
fi
if printf '%s\n' "${images[@]}" | grep -q 'public\.ecr\.aws'; then
  aws ecr-public get-login-password --region us-east-1 2>/dev/null \
    | docker login --username AWS --password-stdin public.ecr.aws >/dev/null 2>&1 \
    || err "warning: could not log into public.ecr.aws"
fi

# ---- Prong A: image arch coverage ----
missing_arch=()
for image in "${images[@]}"; do
  [[ -z "${image}" ]] && continue
  # imagetools prints one `Platform: linux/<arch>` per child manifest; a
  # single-arch image prints exactly one. Fail closed if inspection errors.
  platforms="$(docker buildx imagetools inspect "${image}" 2>/dev/null | grep -oE 'linux/(amd64|arm64)' | sort -u || true)"
  if [[ -z "${platforms}" ]]; then
    missing_arch+=("${image} (could not inspect manifest)")
  elif ! grep -qx "linux/${target_arch}" <<<"${platforms}"; then
    missing_arch+=("${image} (has: $(tr '\n' ' ' <<<"${platforms}"))")
  fi
done

# ---- report ----
status=0

if ((${#missing_arch[@]} > 0)); then
  status=1
  err "FAIL (Prong A): images missing a linux/${target_arch} manifest:"
  for entry in "${missing_arch[@]}"; do
    err "  - ${entry}"
  done
fi

if ((${#bad_pins[@]} > 0)); then
  status=1
  err "FAIL (Prong B): workloads pin kubernetes.io/arch to a non-${target_arch} arch:"
  for entry in "${bad_pins[@]}"; do
    err "  - ${entry}"
  done
fi

if ((status == 0)); then
  echo "arch-readiness OK: all ${#images[@]} workload images publish linux/${target_arch}, no wrong-arch pins."
fi

exit "${status}"
