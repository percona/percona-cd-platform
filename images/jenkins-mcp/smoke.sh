#!/usr/bin/env bash
# Smoke-boot the jenkins-mcp image: start it with an empty fleet in read-only
# mode and assert /healthz serves 200. No Jenkins or Authentik is needed (the
# liveness route returns local state only and OIDC stays off without MCP_OIDC_*).
set -euo pipefail

IMG="${1:?usage: smoke.sh <image-ref>}"

fleet="$(mktemp)"
trap 'rm -f "$fleet"' EXIT # clean the temp file even if docker run fails
echo '{"masters":[]}' >"$fleet"
chmod 0644 "$fleet" # the container runs as uid 65532 and must read the mount

cid="$(docker run -d -p 9887:9887 \
  -e MCP_JENKINS_FLEET_FILE=/etc/jenkins-mcp/fleet.json \
  -v "$fleet:/etc/jenkins-mcp/fleet.json:ro" \
  "$IMG" --transport streamable-http --host 0.0.0.0 --port 9887 --read-only)"
trap 'docker rm -f "$cid" >/dev/null 2>&1 || true; rm -f "$fleet"' EXIT

for _ in $(seq 1 30); do
  if [ "$(docker inspect -f '{{.State.Running}}' "$cid" 2>/dev/null)" != "true" ]; then
    echo "smoke FAILED: container exited early. Logs:"
    docker logs "$cid" || true
    exit 1
  fi
  if curl -fsS http://localhost:9887/healthz >/dev/null 2>&1; then
    echo "smoke OK: /healthz served 200"
    exit 0
  fi
  sleep 2
done

echo "smoke FAILED: /healthz never returned 200. Container logs:"
docker logs "$cid" || true
exit 1
