#!/usr/bin/env bash
# Smoke-boot a built Jenkins controller image and assert the baked Percona fork
# plugins loaded at their LOCKED versions with no plugin-load failures.
#
# Usage: smoke-boot.sh <image-ref>
#
# Modes:
#   empty-home (always): boot with a fresh JENKINS_HOME. The ref-dir contents
#     (community .jpi + the two fork .jpi.override) seed the home, so the running
#     instance reflects exactly what the image baked.
#   restored-home (optional): if SMOKE_RESTORED_HOME_TAR points to a tar of a
#     real, plugin-populated JENKINS_HOME, boot with it extracted into the home
#     to PROVE the .override markers make the baked fork plugins win over the
#     pre-existing PVC copy. Skipped (with a loud note) when the env var is
#     unset, so the empty-home path still gives a full PR-time signal.
set -euo pipefail

IMAGE="${1:?usage: smoke-boot.sh <image-ref>}"
HERE="$(cd "$(dirname "$0")" && pwd)"
LOCK="$HERE/percona-plugins.lock.json"

command -v jq >/dev/null || { echo "jq required" >&2; exit 1; }

# Expected baked forks = lock entries whose sha256 is a real 64-hex string (i.e.
# actually fetched + baked). The EC2 TODO entry is skipped automatically, the
# same gate fetch-hpis.sh applies, so a Hetzner-only image smokes clean today.
mapfile -t EXPECT < <(jq -r '.plugins[] | select(.sha256|test("^[0-9a-f]{64}$")) | "\(.id) \(.version)"' "$LOCK")
if [ "${#EXPECT[@]}" -eq 0 ]; then
  echo "FAIL: no baked fork plugins with a valid sha256 in $LOCK" >&2
  exit 1
fi

run_one() {
  local mode="$1" mount="$2"
  local name="smoke-${mode}-$$"
  echo "=== smoke (${mode}): booting ${IMAGE} ==="
  docker rm -f "$name" >/dev/null 2>&1 || true
  # shellcheck disable=SC2086
  docker run -d --name "$name" \
    -e JAVA_OPTS="-Djenkins.install.runSetupWizard=false" \
    $mount "$IMAGE" >/dev/null

  local up=""
  for _ in $(seq 1 60); do
    if docker logs "$name" 2>&1 | grep -q "Jenkins is fully up and running"; then up=1; break; fi
    if [ -z "$(docker ps -q --filter "name=$name")" ]; then
      echo "FAIL (${mode}): container exited during boot" >&2
      docker logs "$name" 2>&1 | tail -60 >&2
      docker rm -f "$name" >/dev/null 2>&1 || true
      return 1
    fi
    sleep 3
  done
  if [ -z "$up" ]; then
    echo "FAIL (${mode}): Jenkins did not report 'fully up and running' within ~180s" >&2
    docker logs "$name" 2>&1 | tail -60 >&2
    docker rm -f "$name" >/dev/null 2>&1 || true
    return 1
  fi

  # Hard-fail on plugin-load failures.
  if docker logs "$name" 2>&1 | grep -Eq "Failed Loading plugin|Failed to load: |Failed to load plugin"; then
    echo "FAIL (${mode}): plugin load failure in startup log:" >&2
    docker logs "$name" 2>&1 | grep -E "Failed Loading plugin|Failed to load" >&2 || true
    docker rm -f "$name" >/dev/null 2>&1 || true
    return 1
  fi
  # Surface any other SEVERE entries as a warning (non-fatal).
  docker logs "$name" 2>&1 | grep -E "\bSEVERE\b" >&2 && echo "WARN (${mode}): SEVERE entries above (non-fatal)" >&2 || true

  # Assert each baked fork plugin is present at its locked version.
  local rc=0 id ver installed line
  for line in "${EXPECT[@]}"; do
    id="${line% *}"; ver="${line#* }"
    installed=$(docker exec "$name" sh -c \
      "cat /var/jenkins_home/plugins/$id/META-INF/MANIFEST.MF 2>/dev/null | tr -d '\r' | awk -F': ' '/^Plugin-Version:/{print \$2}'" || true)
    if [ "$installed" = "$ver" ]; then
      echo "  ok   $id == $ver"
    else
      echo "  FAIL $id: expected $ver, got '${installed:-<absent>}'" >&2
      rc=1
    fi
  done

  docker rm -f "$name" >/dev/null 2>&1 || true
  return "$rc"
}

RC=0
run_one empty "" || RC=1

if [ -n "${SMOKE_RESTORED_HOME_TAR:-}" ]; then
  if [ -f "$SMOKE_RESTORED_HOME_TAR" ]; then
    TMPH="$(mktemp -d)"
    tar -xf "$SMOKE_RESTORED_HOME_TAR" -C "$TMPH"
    chmod -R 0777 "$TMPH"
    run_one restored "-v $TMPH:/var/jenkins_home" || RC=1
    rm -rf "$TMPH"
  else
    echo "WARN: SMOKE_RESTORED_HOME_TAR=$SMOKE_RESTORED_HOME_TAR not found; skipping restored-home smoke." >&2
  fi
else
  echo "NOTE: SMOKE_RESTORED_HOME_TAR unset; restored-home (.override-wins-over-PVC) smoke SKIPPED." >&2
  echo "      Empty-home smoke ran. Set SMOKE_RESTORED_HOME_TAR to a real JENKINS_HOME tar to exercise .override." >&2
fi

[ "$RC" -eq 0 ] && echo "smoke-boot: PASS" || echo "smoke-boot: FAIL" >&2
exit "$RC"
