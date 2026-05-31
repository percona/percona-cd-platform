#!/usr/bin/env bash
# refresh-fork-locks.sh -- recorded-pin auto-bump for the Percona Jenkins fork plugins.
#
# Reads images/jenkins/percona-plugins.lock.json and, for every plugin entry:
#   1. derives the GitHub owner/repo from that entry's existing release `url`
#      (the lock is the single source of truth; no hard-coded repo map)
#   2. lists the repo's releases (forks are public, so no token is needed to LIST;
#      GH_TOKEN is used only to raise the API rate limit when present), drops
#      drafts, drops prereleases unless ALLOW_PRERELEASE=1, and keeps only tags
#      carrying a `.percona.` fork version (an upstream-synced tag is ignored)
#   3. picks the highest version with `sort -V` (numeric-aware: .9 < .10 < .26)
#   4. only when that is strictly newer than the locked version: downloads the
#      single `<id>-<ver>.hpi` release asset (the `.hpi.sha256` sidecar is NOT a
#      second match -- the glob is anchored `\.hpi$`), integrity-checks it with
#      `unzip -t`, verifies the embedded MANIFEST `Short-Name`==id and
#      `Plugin-Version`==ver, computes sha256 (cross-checked against the published
#      `.hpi.sha256` sidecar when present), then rewrites that lock entry
#      (version/filename/url/sha256).
#
# It NEVER edits the Dockerfile, fetch-hpis.sh, or the values-base image tag: the
# build stays pinned+sha-verified+smoke-asserted and the DEPLOY stays manual
# (ADR 0025). The CI wrapper (.github/workflows/refresh-fork-locks.yml) re-runs
# fetch + build + smoke against the rewritten lock, then opens a PR; CODEOWNERS
# approves. No auto-merge, no auto-bump of the deployed image tag.
#
# Usage:
#   scripts/refresh-fork-locks.sh            rewrite the lock in place (if newer)
#   scripts/refresh-fork-locks.sh --check    report only; exit 3 if a bump exists
#   ALLOW_PRERELEASE=1 scripts/refresh-fork-locks.sh   include prereleases
set -euo pipefail

cd "$(dirname "$0")/.."
LOCK="images/jenkins/percona-plugins.lock.json"
ALLOW_PRERELEASE="${ALLOW_PRERELEASE:-0}"
CHECK_ONLY=0
[ "${1:-}" = "--check" ] && CHECK_ONLY=1

command -v jq    >/dev/null || { echo "jq required" >&2; exit 1; }
command -v curl  >/dev/null || { echo "curl required" >&2; exit 1; }
command -v unzip >/dev/null || { echo "unzip required" >&2; exit 1; }
[ -f "$LOCK" ] || { echo "lock not found: $LOCK" >&2; exit 1; }

AUTH=()
[ -n "${GH_TOKEN:-}" ] && AUTH=(-H "Authorization: Bearer $GH_TOKEN")
api() { curl -fsSL "${AUTH[@]}" -H "Accept: application/vnd.github+json" "$@"; }

changed=0
count=$(jq '.plugins | length' "$LOCK")
[ "$count" -gt 0 ] || { echo "lock has zero plugins" >&2; exit 1; }

for i in $(seq 0 $((count - 1))); do
  id=$(jq -r ".plugins[$i].id" "$LOCK")
  cur=$(jq -r ".plugins[$i].version" "$LOCK")
  url=$(jq -r ".plugins[$i].url" "$LOCK")

  # owner/repo from https://github.com/<owner>/<repo>/releases/download/...
  repo=$(printf '%s' "$url" | sed -E 's#^https://github.com/([^/]+/[^/]+)/releases/.*#\1#')
  [ "$repo" != "$url" ] || { echo "FAIL: $id cannot parse owner/repo from url: $url" >&2; exit 1; }

  releases=$(api "https://api.github.com/repos/$repo/releases?per_page=100")

  latest=$(printf '%s' "$releases" | jq -r --argjson pre "$ALLOW_PRERELEASE" '
      .[]
      | select(.draft == false)
      | select($pre == 1 or .prerelease == false)
      | .tag_name
      | select(test("\\.percona\\."))
      | ltrimstr("v")' | sort -V | tail -1)
  [ -n "$latest" ] || { echo "FAIL: $id ($repo) no eligible (.percona.) release found" >&2; exit 1; }

  # Strict-newer gate: skip equality AND any case where the lock is already >=
  # the newest published release (manual pin ahead of releases must not downgrade).
  newest=$(printf '%s\n%s\n' "$cur" "$latest" | sort -V | tail -1)
  if [ "$latest" = "$cur" ] || [ "$newest" != "$latest" ]; then
    echo "UP-TO-DATE $id $cur"
    continue
  fi

  echo "BUMP $id $cur -> $latest"
  changed=1
  [ "$CHECK_ONLY" -eq 1 ] && continue

  tag="v$latest"
  fname="$id-$latest.hpi"

  # Exactly one real HPI asset (anchored \.hpi$ excludes the .hpi.sha256 sidecar).
  hpi_count=$(printf '%s' "$releases" | jq -r --arg t "$tag" --arg id "$id" '
      [ .[] | select(.tag_name == $t) | .assets[] | select(.name | test("^" + $id + "-.*\\.hpi$")) ] | length')
  [ "$hpi_count" = "1" ] || { echo "FAIL: $id $tag expected exactly one <id>-*.hpi asset, got $hpi_count" >&2; exit 1; }

  asset_url=$(printf '%s' "$releases" | jq -r --arg t "$tag" --arg f "$fname" '
      .[] | select(.tag_name == $t) | .assets[] | select(.name == $f) | .browser_download_url')
  [ -n "$asset_url" ] || { echo "FAIL: $id $tag missing asset $fname" >&2; exit 1; }

  tmp=$(mktemp)
  trap 'rm -f "$tmp"' RETURN 2>/dev/null || true
  curl -fsSL --retry 3 --retry-delay 2 -o "$tmp" "$asset_url"

  unzip -tqq "$tmp" >/dev/null || { echo "FAIL: $id $latest corrupt HPI (unzip -t failed)" >&2; rm -f "$tmp"; exit 1; }

  man=$(unzip -p "$tmp" META-INF/MANIFEST.MF | tr -d '\r')
  m_id=$(printf '%s' "$man" | awk -F': ' '/^Short-Name:/{print $2; exit}')
  m_ver=$(printf '%s' "$man" | awk -F': ' '/^Plugin-Version:/{print $2; exit}')
  [ "$m_id" = "$id" ]      || { echo "FAIL: $id MANIFEST Short-Name='$m_id' (expected $id)" >&2; rm -f "$tmp"; exit 1; }
  [ "$m_ver" = "$latest" ] || { echo "FAIL: $id MANIFEST Plugin-Version='$m_ver' (expected $latest)" >&2; rm -f "$tmp"; exit 1; }

  sha=$(sha256sum "$tmp" | awk '{print $1}')

  # Defense in depth: if the release publishes a <fname>.sha256 sidecar, it must agree.
  side_url=$(printf '%s' "$releases" | jq -r --arg t "$tag" --arg f "$fname.sha256" '
      .[] | select(.tag_name == $t) | .assets[] | select(.name == $f) | .browser_download_url')
  if [ -n "$side_url" ]; then
    want=$(curl -fsSL "$side_url" | awk '{print $1}')
    if [ -n "$want" ] && [ "$want" != "$sha" ]; then
      echo "FAIL: $id $latest sha256 disagrees with published sidecar" >&2
      echo "  computed $sha" >&2
      echo "  sidecar  $want" >&2
      rm -f "$tmp"; exit 1
    fi
  fi
  rm -f "$tmp"

  newurl="https://github.com/$repo/releases/download/$tag/$fname"
  jq --argjson i "$i" --arg v "$latest" --arg f "$fname" --arg u "$newurl" --arg s "$sha" '
      .plugins[$i].version  = $v
    | .plugins[$i].filename = $f
    | .plugins[$i].url      = $u
    | .plugins[$i].sha256   = $s' "$LOCK" > "$LOCK.tmp" && mv "$LOCK.tmp" "$LOCK"
  echo "  locked $id $latest sha256=$sha"
done

if [ "$CHECK_ONLY" -eq 1 ]; then
  if [ "$changed" -eq 1 ]; then echo "refresh-fork-locks: bump available (--check)"; exit 3; fi
  echo "refresh-fork-locks: up to date"; exit 0
fi
[ "$changed" -eq 1 ] && echo "refresh-fork-locks: lock updated" || echo "refresh-fork-locks: no changes"
exit 0
