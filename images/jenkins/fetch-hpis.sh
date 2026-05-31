#!/usr/bin/env bash
# Download the patched fork HPIs into ./percona-plugins/, verifying each against
# the committed lock file. Fails on any mismatch. Run from images/jenkins/ before
# `docker buildx build` (the CI workflow runs it as a build-prep step).
#
# No network writes happen until AFTER the sha is checked: download to a temp
# file, verify, then move into place. A tampered/mismatched asset never lands.
set -euo pipefail

cd "$(dirname "$0")"
LOCK="percona-plugins.lock.json"
DEST="percona-plugins"
mkdir -p "$DEST"

command -v jq >/dev/null || { echo "jq required" >&2; exit 1; }

count=$(jq '.plugins | length' "$LOCK")
[ "$count" -gt 0 ] || { echo "lock file has zero plugins" >&2; exit 1; }

for i in $(seq 0 $((count - 1))); do
  id=$(jq -r ".plugins[$i].id" "$LOCK")
  version=$(jq -r ".plugins[$i].version" "$LOCK")
  filename=$(jq -r ".plugins[$i].filename" "$LOCK")
  url=$(jq -r ".plugins[$i].url" "$LOCK")
  want=$(jq -r ".plugins[$i].sha256" "$LOCK")

  # Hard-fail on an unfilled / malformed sha (e.g. the EC2 TODO placeholder).
  if ! printf '%s' "$want" | grep -Eq '^[0-9a-f]{64}$'; then
    echo "FAIL: $id $version has no valid sha256 in $LOCK (got '$want')" >&2
    echo "      (EC2 entry is gated on FK 3.1 cutting v5.24.percona.3.)" >&2
    exit 1
  fi

  tmp=$(mktemp)
  echo "fetch: $id $version <- $url"
  curl -fsSL --retry 3 --retry-delay 2 -o "$tmp" "$url"

  got=$(sha256sum "$tmp" | awk '{print $1}')
  if [ "$got" != "$want" ]; then
    echo "FAIL: $id $version sha256 mismatch" >&2
    echo "  want $want" >&2
    echo "  got  $got" >&2
    rm -f "$tmp"
    exit 1
  fi

  # Stage as <id>.hpi (the plugin short-name), NOT the versioned release-asset
  # name: Jenkins derives the plugin id from the ref-dir filename, so the file
  # MUST be <short-name>.hpi for the COPY into /usr/share/jenkins/ref/plugins
  # and the .override markers to bind to the correct plugin. "filename" stays in
  # the lock only as the remote asset name embedded in "url".
  mv "$tmp" "$DEST/$id.hpi"
  echo "  ok   $id.hpi (from $filename, $got)"
done

echo "All fork HPIs fetched + verified into $DEST/"
