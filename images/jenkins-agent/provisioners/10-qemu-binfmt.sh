#!/bin/bash
# arm64 capability profile: bake qemu user-mode emulation so x86_64-only
# containers (the PXB Minio/Vault/KMIP test helpers) run on Graviton workers.
# AL2023 does not package qemu-user-static, so the static binary comes from
# the Percona ECR Public mirror of tonistiigi/binfmt, pinned by index digest
# and by the extracted binary's BuildID.
#
# Registration is PERSISTENT: /etc/binfmt.d + systemd-binfmt, not a boot-time
# /proc write. The F (fix-binary) flag loads the interpreter at registration
# so containers can exec it without the binary existing in their filesystem.
set -euo pipefail

readonly QEMU_IMAGE="public.ecr.aws/e7j3v3n0/qemu-binfmt@sha256:d3b963f787999e6c0219a48dba02978769286ff61a5f4d26245cb6a6e5567ea3"
readonly QEMU_BUILD_ID="3ce82273ab59ab77b04b0bee9060c5b194769f4c"
readonly QEMU_BIN="/usr/local/bin/qemu-x86_64"

if [[ "$(uname -m)" != "aarch64" ]]; then
  echo "10-qemu-binfmt: x86_64 host, nothing to do"
  exit 0
fi

# Bounded retry: a bake must fail loudly, never hang on a throttled pull.
for attempt in 1 2 3 4 5; do
  if sudo docker pull --platform linux/arm64 "${QEMU_IMAGE}" >/dev/null; then
    break
  fi

  if [[ "${attempt}" -eq 5 ]]; then
    echo "FATAL: qemu image pull failed after ${attempt} attempts" >&2
    exit 1
  fi

  sleep $((attempt * 10))
done

container_id="$(sudo docker create --platform linux/arm64 "${QEMU_IMAGE}")"
sudo docker cp "${container_id}:/usr/bin/qemu-x86_64" "${QEMU_BIN}"
sudo docker rm -f "${container_id}" >/dev/null
sudo chmod 0755 "${QEMU_BIN}"

# file(1) is installed by 00-common.sh: without it this check can never match
# and a healthy binary would be rejected.
if ! file "${QEMU_BIN}" | grep -Fq "BuildID[sha1]=${QEMU_BUILD_ID}"; then
  echo "FATAL: ${QEMU_BIN} BuildID mismatch (expected ${QEMU_BUILD_ID})" >&2
  exit 1
fi

# printf, never echo: the magic contains NUL escapes that echo can truncate.
printf '%s\n' ':qemu-x86_64:M::\x7fELF\x02\x01\x01\x00\x00\x00\x00\x00\x00\x00\x00\x00\x02\x00\x3e\x00:\xff\xff\xff\xff\xff\xfe\xfe\x00\xff\xff\xff\xff\xff\xff\xff\xff\xfe\xff\xff\xff:/usr/local/bin/qemu-x86_64:POCF' \
  | sudo tee /etc/binfmt.d/qemu-x86_64.conf >/dev/null

sudo systemctl enable systemd-binfmt.service
sudo systemctl restart systemd-binfmt.service

if [[ ! -e /proc/sys/fs/binfmt_misc/qemu-x86_64 ]]; then
  echo "FATAL: binfmt registration missing after systemd-binfmt restart" >&2
  exit 1
fi

emulated_arch="$(sudo docker run --rm --platform linux/amd64 public.ecr.aws/amazonlinux/amazonlinux:2023 uname -m)"

if [[ "${emulated_arch}" != "x86_64" ]]; then
  echo "FATAL: emulation check returned ${emulated_arch}, expected x86_64" >&2
  exit 1
fi

echo "10-qemu-binfmt: qemu ${QEMU_BUILD_ID} registered, x86_64 emulation verified"
