#!/bin/bash
# Assertion-only smoke payload, run on a fresh boot of the candidate AMI.
# Every check mirrors something a worker label family does on its first
# build: toolchain presence, a working docker daemon, and (arm64) x86_64
# emulation surviving a reboot cycle via systemd-binfmt.
set -euo pipefail

for tool in java git docker aws file tar unzip 7za curl; do
  command -v "${tool}" >/dev/null || { echo "FATAL: ${tool} missing" >&2; exit 1; }
done

case "${SMOKE_ARCH:?}" in
  x86_64) expected_native_arch=x86_64 ;;
  arm64) expected_native_arch=aarch64 ;;
  *) echo "FATAL: Unsupported architecture ${SMOKE_ARCH}" >&2; exit 1 ;;
esac
if [[ "$(uname -m)" != "${expected_native_arch}" ]]; then
  echo "FATAL: native architecture does not match ${SMOKE_ARCH}" >&2
  exit 1
fi

java_properties="$(java -XshowSettings:properties -version 2>&1)"
java_major="$(printf '%s\n' "${java_properties}" | awk '$1 == "java.specification.version" { print $3 }')"
if [[ "${java_major}" != "21" ]]; then
  echo "FATAL: Java 21 is required by this agent image, found ${java_major}" >&2
  exit 1
fi

for document in LICENSE NOTICE README.md LICENSE.qemu; do
  test -s "/licenses/${document}" || { echo "FATAL: /licenses/${document} missing" >&2; exit 1; }
done

sudo systemctl is-enabled docker >/dev/null
sudo systemctl is-active docker >/dev/null

# Run as the SSH/agent user, so a broken docker group is not hidden by sudo.
docker run --rm public.ecr.aws/amazonlinux/amazonlinux:2023 true

# Loading the target controller's remoting checks JVM compatibility, not a
# Jenkins connection. A separately authorized worker canary still gates adoption.
agent_jar="$(mktemp)"
trap 'rm -f "${agent_jar}"' EXIT
curl --fail --silent --show-error --proto '=https' --retry 3 \
  --connect-timeout 10 --max-time 120 \
  "${SMOKE_JENKINS_URL:?}/jnlpJars/agent.jar" --output "${agent_jar}"
java -jar "${agent_jar}" -version

if [[ "${SMOKE_ARCH:?}" == "arm64" ]]; then
  [[ -e /proc/sys/fs/binfmt_misc/qemu-x86_64 ]] || { echo "FATAL: binfmt registration absent on fresh boot" >&2; exit 1; }

  emulated_arch="$(docker run --rm --platform linux/amd64 public.ecr.aws/amazonlinux/amazonlinux:2023 uname -m)"

  if [[ "${emulated_arch}" != "x86_64" ]]; then
    echo "FATAL: emulation returned ${emulated_arch}, expected x86_64" >&2
    exit 1
  fi
fi

echo "smoke: all assertions passed (${SMOKE_ARCH})"
