#!/usr/bin/env bash
# Write the effective plugin inventory of a built controller image, one
# "id:version" line per plugin, as the entrypoint would seed it: plain .jpi
# files plus the .jpi.override forks and pinned plugins. Versions come from each
# archive's MANIFEST.MF, so the list reflects the final image, not plugins.txt.
#
# usage: plugin-inventory.sh <image-ref> <output-file>
set -euo pipefail

image="${1:?image reference required}"
out="${2:?output file required}"

work="$(mktemp -d)"
cid=""
cleanup() {
  [[ -n "${cid}" ]] && docker rm -f "${cid}" >/dev/null 2>&1
  rm -rf "${work}"
}
trap cleanup EXIT

cid="$(docker create "${image}")"
docker cp "${cid}:/usr/share/jenkins/ref/plugins" "${work}/plugins"

: > "${work}/inventory"
shopt -s nullglob
for archive in "${work}"/plugins/*.jpi "${work}"/plugins/*.jpi.override; do
  name="$(basename "${archive}")"
  id="${name%.override}"
  id="${id%.jpi}"
  version="$(unzip -p "${archive}" META-INF/MANIFEST.MF \
    | tr -d '\r' | sed -n 's/^Plugin-Version: //p' | head -n 1)"
  if [[ -z "${version}" ]]; then
    echo "no Plugin-Version in ${name}" >&2
    exit 1
  fi
  echo "${id}:${version}" >> "${work}/inventory"
done

count="$(wc -l < "${work}/inventory")"
if (( count == 0 )); then
  echo "no plugins found in ${image}" >&2
  exit 1
fi
sort "${work}/inventory" > "${out}"
echo "wrote ${count} plugins to ${out}"
