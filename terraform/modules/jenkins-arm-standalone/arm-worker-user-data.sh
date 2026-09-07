#!/bin/bash
# ARM (Graviton) Jenkins worker bootstrap for the ec2-fleet spot pool.
# Runs as root at boot via cloud-init. Arch-aware (no hardcoded x86_64), and it
# chowns the agent remoteFS to ec2-user since the ec2-fleet plugin's SSH launcher
# logs in as ec2-user (the legacy ec2-plugin ran its initScript as ec2-user, so
# /mnt/jenkins ended up ec2-user-owned; we reproduce that here).
set -o xtrace

# Mount the largest unmounted block device (the data volume) at /mnt.
if ! mountpoint -q /mnt; then
    DEVICE=""
    for DEVICE_NAME in $(lsblk -ndpbo NAME,SIZE | sort -n -r | awk '{print $1}'); do
        if ! grep -qs "${DEVICE_NAME}" /proc/mounts; then
            DEVICE="${DEVICE_NAME}"
            break
        fi
    done
    if [ -n "${DEVICE}" ]; then
        mkfs.ext4 "${DEVICE}"
        mount -o noatime "${DEVICE}" /mnt
    fi
fi

ethtool -K eth0 sg off || true
until yum makecache; do sleep 1; echo try again; done

yum -y install java-21-amazon-corretto-headless tzdata-java unzip || yum -y install java-21-openjdk-headless tzdata-java unzip || :
yum -y install git docker
yum -y remove awscli || :

# awscli v2, arch-aware ($(uname -m) -> aarch64), not the hardcoded x86_64 URL.
if ! aws --version 2>/dev/null | grep -q 'aws-cli/2'; then
    find /tmp -maxdepth 1 -name "*aws*" | xargs rm -rf || true
    until curl "https://awscli.amazonaws.com/awscli-exe-linux-$(uname -m).zip" -o "/tmp/awscliv2.zip"; do sleep 1; echo try again; done
    unzip -q -o /tmp/awscliv2.zip -d /tmp
    (cd /tmp/aws && ./install)
fi

# Agent remoteFS, owned by the SSH login user (ec2-user).
install -o ec2-user -g ec2-user -d /mnt/jenkins

# Internal package repo.
echo '10.30.6.9 repo.ci.percona.com' >> /etc/hosts

sysctl net.ipv4.tcp_fin_timeout=15 || true
sysctl net.ipv4.tcp_tw_reuse=1 || true
sysctl net.ipv6.conf.all.disable_ipv6=1 || true
sysctl net.ipv6.conf.default.disable_ipv6=1 || true
sysctl -w fs.inotify.max_user_watches=10000000 || true
sysctl -w fs.aio-max-nr=1048576 || true
sysctl -w fs.file-max=6815744 || true
echo "*  soft  core  unlimited" >> /etc/security/limits.conf

echo 'DOCKER_STORAGE_OPTIONS="--data-root=/mnt/docker"' >> /etc/sysconfig/docker-storage || true
sed -i.bak -e 's^ExecStart=.*^ExecStart=/usr/bin/dockerd --data-root=/mnt/docker --default-ulimit nofile=900000:900000^' /usr/lib/systemd/system/docker.service || true
systemctl daemon-reload || true
install -o root -g root -d /mnt/docker
usermod -aG docker ec2-user
mkdir -p /etc/docker
echo '{"experimental": true, "ipv6": true, "fixed-cidr-v6": "fd3c:a8b0:18eb:5c06::/64"}' > /etc/docker/daemon.json
systemctl enable --now docker || systemctl start docker
