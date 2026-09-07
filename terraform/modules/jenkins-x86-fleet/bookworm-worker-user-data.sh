#!/bin/bash
# Debian 12 (bookworm) Jenkins worker bootstrap for the x86_64 ec2-fleet spot
# pool. Runs as root at boot via cloud-init. Ports the classic ec2-plugin
# min-* initScript (which ran as the AMI login user via sudo) and adds the
# ec2-user login shim: the ec2-fleet plugin's SSH launcher authenticates as
# ec2-user (the fleet-wide percona-jenkins credential), while Debian cloud
# images only create `admin`. Fails closed: any unmet postcondition aborts
# the boot, the instance never joins Jenkins, and the plugin replaces it.
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

# SSH login shim (ec2-user) FIRST, before any package work, so the instance
# is reachable as the plugin's login user even while later steps retry.
# cloud-init installs the key pair for admin before user scripts run; the
# bounded wait covers slow key delivery, the assert fails the boot if the
# key never arrives.
for _ in $(seq 1 30); do
  if [[ -s /home/admin/.ssh/authorized_keys ]]; then
    break
  fi
  sleep 2
done

[[ -s /home/admin/.ssh/authorized_keys ]]

if ! id ec2-user >/dev/null 2>&1; then
  useradd -m -s /bin/bash ec2-user
fi

install -o ec2-user -g ec2-user -m 700 -d /home/ec2-user/.ssh
install -o ec2-user -g ec2-user -m 600 /home/admin/.ssh/authorized_keys /home/ec2-user/.ssh/authorized_keys
echo 'ec2-user ALL=(ALL) NOPASSWD:ALL' > /etc/sudoers.d/ec2-user
chmod 440 /etc/sudoers.d/ec2-user

# Agent remoteFS, owned by the SSH login user.
install -o ec2-user -g ec2-user -d /mnt/jenkins

# Bounded retry (5 min): a longer repo outage aborts the boot and the plugin
# replaces the instance instead of it looping forever.
for _ in $(seq 1 60); do
  if DEBIAN_FRONTEND=noninteractive apt-get update; then
    break
  fi
  sleep 5
  echo "try again"
done

# The JRE install can fail in the ca-certificates-java postinst; the
# /etc/ssl move-and-restore retry is carried over from the proven classic
# initScript for this AMI family. Each step tolerates failure, the
# postcondition assert below is the real gate.
DEBIAN_FRONTEND=noninteractive apt-get -y install openjdk-17-jre-headless git || true

if ! command -v java >/dev/null; then
  mv /etc/ssl /etc/ssl_old || true
  DEBIAN_FRONTEND=noninteractive apt-get -y install openjdk-17-jre-headless || true
  cp -r /etc/ssl_old /etc/ssl || true
  DEBIAN_FRONTEND=noninteractive apt-get -y install openjdk-17-jre-headless || true
fi

# Internal package repo.
echo '10.30.6.9 repo.ci.percona.com' >> /etc/hosts

# Postconditions: every capability the agent contract needs, asserted under
# set -e so a violation aborts the boot loudly.
command -v java >/dev/null
command -v git >/dev/null
mountpoint -q /mnt
echo "BOOTSTRAP-OK"
