#!/bin/bash
# Amazon Linux Jenkins docker-worker bootstrap for the x86_64 ec2-fleet spot
# pool. Runs as root at boot via cloud-init. Ports the classic ec2-plugin
# docker initScript (java, git, docker with /mnt/docker data-root, awscli v2,
# cronie) for the ec2-fleet SSH launcher, which logs in as ec2-user (native
# on this AMI family). Arch-aware, no hardcoded x86_64. Fails closed: any
# unmet postcondition aborts the boot, the instance never joins Jenkins, and
# the plugin replaces it.
set -euo pipefail
set -o xtrace

# Mount the data volume at /mnt, selected by EBS identity, never by name or
# size heuristics: on d-type instances the NVMe instance store would shadow
# it, and NVMe device names are attach-order dependent. The bounded wait
# covers late volume attachment; the assert fails the boot if it never shows.
if ! mountpoint -q /mnt; then
  device=""
  # The whole disk backing the root filesystem, e.g. nvme0n1. Positively
  # excluded from the pick so no naming convention can ever make it win.
  root_disk="$(lsblk -no PKNAME "$(findmnt -n -o SOURCE /)")"

  for _ in $(seq 1 30); do
    for candidate in /dev/disk/by-id/nvme-Amazon_Elastic_Block_Store_*; do
      if [[ ! -e "${candidate}" ]]; then
        continue
      fi

      # Whole-disk links only: the root volume's partition links resolve to
      # unmounted slices (the 3 MB BIOS-boot partition) and must never win
      # the pick. Namespace-qualified aliases resolve to whole disks; they
      # are skipped as duplicates of the base link.
      if [[ "${candidate}" == *-part* || "${candidate}" == *-ns-* ]]; then
        continue
      fi

      resolved="$(readlink -f "${candidate}")"

      if [[ "$(basename "${resolved}")" == "${root_disk}" ]]; then
        continue
      fi

      if ! grep -qs "${resolved}" /proc/mounts; then
        device="${resolved}"
        break 2
      fi
    done

    # Xen device naming has no EBS by-id symlinks; fall back to the declared
    # data device.
    if [[ -b /dev/xvdd ]] && ! grep -qs /dev/xvdd /proc/mounts; then
      device=/dev/xvdd
      break
    fi

    sleep 2
  done

  [[ -n "${device}" ]]
  mkfs.ext4 "${device}"
  mount -o noatime "${device}" /mnt
fi

# The data volume is never smaller than 20 GiB; a tiny /mnt means the device
# pick grabbed the wrong block device. Asserted before the SSH login user
# exists, so a wrong pick can never yield a connectable agent.
mnt_size_gb="$(df -BG --output=size /mnt | tail -1 | tr -d ' G')"
[[ "${mnt_size_gb}" -ge 20 ]]

ethtool -K eth0 sg off || true

# Bounded retry (5 min): a longer repo outage aborts the boot and the plugin
# replaces the instance instead of it looping forever.
for _ in $(seq 1 60); do
  if yum makecache; then
    break
  fi
  sleep 5
  echo "try again"
done

# Corretto first, OpenJDK as the fallback; the postcondition assert below is
# the real gate.
yum -y install java-17-amazon-corretto-headless tzdata-java cronie unzip ||
  yum -y install java-17-openjdk-headless tzdata-java cronie unzip || true
yum -y install git docker
yum -y remove awscli || true

systemctl enable --now crond

# awscli v2, arch-aware ($(uname -m) resolves x86_64 or aarch64).
if ! aws --version 2>/dev/null | grep -q 'aws-cli/2'; then
  find /tmp -maxdepth 1 -name "*aws*" -exec rm -rf {} +

  for _ in $(seq 1 60); do
    if curl "https://awscli.amazonaws.com/awscli-exe-linux-$(uname -m).zip" -o /tmp/awscliv2.zip; then
      break
    fi
    sleep 5
    echo "try again"
  done

  unzip -q -o /tmp/awscliv2.zip -d /tmp
  (cd /tmp/aws && ./install)
fi

# Agent remoteFS, owned by the SSH login user (ec2-user).
install -o ec2-user -g ec2-user -d /mnt/jenkins

# Internal package repo.
echo '10.30.6.9 repo.ci.percona.com' >> /etc/hosts

# Kernel tuning carried over from the classic docker initScript; each knob is
# idempotent and kernel-dependent, so failures are masked individually.
sysctl net.ipv4.tcp_fin_timeout=15 || true
sysctl net.ipv4.tcp_tw_reuse=1 || true
sysctl net.ipv6.conf.all.disable_ipv6=1 || true
sysctl net.ipv6.conf.default.disable_ipv6=1 || true
sysctl -w fs.inotify.max_user_watches=10000000 || true
sysctl -w fs.aio-max-nr=1048576 || true
sysctl -w fs.file-max=6815744 || true
echo '*  soft  core  unlimited' >> /etc/security/limits.conf

# Docker with its data on the /mnt volume.
echo 'DOCKER_STORAGE_OPTIONS="--data-root=/mnt/docker"' >> /etc/sysconfig/docker-storage || true
sed -i.bak -e 's^ExecStart=.*^ExecStart=/usr/bin/dockerd --data-root=/mnt/docker --default-ulimit nofile=900000:900000^' /usr/lib/systemd/system/docker.service
systemctl daemon-reload
install -o root -g root -d /mnt/docker
usermod -aG docker ec2-user
mkdir -p /etc/docker
echo '{"experimental": true, "ipv6": true, "fixed-cidr-v6": "fd3c:a8b0:18eb:5c06::/64"}' > /etc/docker/daemon.json
systemctl enable --now docker

# Postconditions: every capability the agent contract needs, asserted under
# set -e so a violation aborts the boot loudly.
command -v java >/dev/null
command -v git >/dev/null
aws --version | grep -q 'aws-cli/2'
mountpoint -q /mnt
docker info >/dev/null
echo "BOOTSTRAP-OK"
