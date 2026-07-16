#!/bin/bash
# Assertion-only smoke payload, run on a fresh boot of the candidate AMI.
# Every check mirrors something a worker label family does on its first
# build: toolchain presence, a working docker daemon, and (arm64) x86_64
# emulation surviving a reboot cycle via systemd-binfmt.
set -euo pipefail

for tool in java git docker aws file; do
  command -v "${tool}" >/dev/null || { echo "FATAL: ${tool} missing" >&2; exit 1; }
done

sudo systemctl is-enabled docker >/dev/null
sudo systemctl is-active docker >/dev/null

sudo docker run --rm public.ecr.aws/amazonlinux/amazonlinux:2023 true

if [[ "${SMOKE_ARCH:?}" == "arm64" ]]; then
  [[ -e /proc/sys/fs/binfmt_misc/qemu-x86_64 ]] || { echo "FATAL: binfmt registration absent on fresh boot" >&2; exit 1; }

  emulated_arch="$(sudo docker run --rm --platform linux/amd64 public.ecr.aws/amazonlinux/amazonlinux:2023 uname -m)"

  if [[ "${emulated_arch}" != "x86_64" ]]; then
    echo "FATAL: emulation returned ${emulated_arch}, expected x86_64" >&2
    exit 1
  fi
fi

echo "smoke: all assertions passed (${SMOKE_ARCH})"
