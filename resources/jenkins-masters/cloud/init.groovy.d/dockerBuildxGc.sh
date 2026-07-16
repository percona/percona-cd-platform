#!/bin/bash
# GC payload for leaked docker buildx builders, executed ON a worker by the
# dockerBuildxGc.groovy controller task (same directory). Jenkins only
# evaluates *.groovy in init.groovy.d, so this file is inert on the master.
#
# Removes ANONYMOUS buildx_buildkit_* containers older than 24h plus their
# <container>_state volumes, sweeps orphaned state volumes, then runs
# age-bounded cache prunes. PROTECTED named builders (shared, long-lived,
# reused by pipelines) are never removed, only cache-pruned. No
# `docker system prune` here: it can race a concurrent job between image
# build and push. Emits one DOCKERBUILDXGC summary line the controller
# parses, or a classified SKIP reason.
set -euo pipefail

command -v docker >/dev/null 2>&1 || { echo "DOCKERBUILDXGC SKIP reason=no-docker-cli"; exit 0; }

if ! docker_info_err="$(docker info 2>&1 >/dev/null)"; then

  if [[ "${docker_info_err}" == *"ermission denied"* ]]; then
    echo "DOCKERBUILDXGC SKIP reason=permission-denied"
  else
    echo "DOCKERBUILDXGC SKIP reason=daemon-unreachable"
  fi

  exit 0
fi

readonly AGE_SECS=$((24 * 3600))
readonly CACHE_UNTIL=48h
readonly PROTECTED_RE='^buildx_buildkit_(multiarch|pmmbuilder)[0-9]*$'
readonly PROTECTED_BUILDERS='multiarch pmmbuilder'
now="$(date +%s)"
removed_containers=0
removed_volumes=0
parse_errors=0

# Stale ANONYMOUS buildkit containers (running or stopped), each with its
# deterministic <name>_state volume. The docker name filter is
# substring-based, so anchor explicitly; protected names skip outright.
while IFS= read -r container; do
  [[ -n "${container}" ]] || continue
  [[ "${container}" == buildx_buildkit_* ]] || continue
  [[ "${container}" =~ ${PROTECTED_RE} ]] && continue
  created_at="$(docker inspect -f '{{.Created}}' "${container}" 2>/dev/null)" || { parse_errors=$((parse_errors + 1)); continue; }
  created_epoch="$(date -d "${created_at}" +%s 2>/dev/null)" || { parse_errors=$((parse_errors + 1)); continue; }
  (( now - created_epoch >= AGE_SECS )) || continue

  if docker rm -f "${container}" >/dev/null 2>&1; then
    removed_containers=$((removed_containers + 1))

    if docker volume rm -f "${container}_state" >/dev/null 2>&1; then
      removed_volumes=$((removed_volumes + 1))
    fi
  fi
done < <(docker ps -a --filter 'name=buildx_buildkit' --format '{{.Names}}')

# Orphaned _state volumes whose container is already gone (same anchoring
# and protection rules; the volume filter is also substring-based).
while IFS= read -r volume; do
  [[ -n "${volume}" ]] || continue
  [[ "${volume}" == buildx_buildkit_*_state ]] || continue
  container="${volume%_state}"
  [[ "${container}" =~ ${PROTECTED_RE} ]] && continue
  docker container inspect "${container}" >/dev/null 2>&1 && continue
  volume_created="$(docker volume inspect -f '{{.CreatedAt}}' "${volume}" 2>/dev/null)" || { parse_errors=$((parse_errors + 1)); continue; }
  volume_epoch="$(date -d "${volume_created}" +%s 2>/dev/null)" || { parse_errors=$((parse_errors + 1)); continue; }
  (( now - volume_epoch >= AGE_SECS )) || continue

  if docker volume rm -f "${volume}" >/dev/null 2>&1; then
    removed_volumes=$((removed_volumes + 1))
  fi
done < <(docker volume ls -q --filter 'name=buildx_buildkit')

# Bounded engine build-cache prune (classic docker build; independent of
# buildx instances and of the per-user current-builder pointer).
prune_output="$(docker builder prune -f --filter "until=${CACHE_UNTIL}" 2>/dev/null)" || prune_output=""
cache_freed="$(awk -F': ' '/Total reclaimed space/{print $2}' <<<"${prune_output}")"
[[ -n "${cache_freed}" ]] || cache_freed="0B"

# Bound each protected named builder's cache without touching the builder.
for builder in ${PROTECTED_BUILDERS}; do
  docker buildx inspect "${builder}" >/dev/null 2>&1 || continue
  named_prune="$(docker buildx --builder "${builder}" prune -f --filter "until=${CACHE_UNTIL}" 2>/dev/null)" || continue
  named_freed="$(awk -F': ' '/Total reclaimed space/{print $2}' <<<"${named_prune}")"
  [[ -n "${named_freed}" ]] || named_freed="0B"
  echo "DOCKERBUILDXGC_NAMED builder=${builder} cache_freed=${named_freed}"
done

echo "DOCKERBUILDXGC containers=${removed_containers} volumes=${removed_volumes} parse_errors=${parse_errors} cache=${cache_freed}"
