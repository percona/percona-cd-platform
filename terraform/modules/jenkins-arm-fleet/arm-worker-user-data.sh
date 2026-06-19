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

if command -v amazon-linux-extras >/dev/null 2>&1; then
    amazon-linux-extras install epel -y || true
fi
yum -y install java-17-amazon-corretto-headless tzdata-java || yum -y install java-17-openjdk-headless tzdata-java || :
yum -y install git docker p7zip
yum -y remove awscli || :

# awscli v2, arch-aware ($(uname -m) -> aarch64), not the hardcoded x86_64 URL.
if ! aws --version 2>/dev/null | grep -q 'aws-cli/2'; then
    find /tmp -maxdepth 1 -name "*aws*" -exec rm -rf {} + || true
    until curl "https://awscli.amazonaws.com/awscli-exe-linux-$(uname -m).zip" -o "/tmp/awscliv2.zip"; do sleep 1; echo try again; done
    7za -aoa -o/tmp x /tmp/awscliv2.zip
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
sed -i.bak -e 's^ExecStart=.*^ExecStart=/usr/bin/dockerd --data-root=/mnt/docker --default-ulimit nofile=900000:900000^' /lib/systemd/system/docker.service || true
systemctl daemon-reload || true
install -o root -g root -d /mnt/docker
usermod -aG docker ec2-user
mkdir -p /etc/docker
echo '{"experimental": true, "ipv6": true, "fixed-cidr-v6": "fd3c:a8b0:18eb:5c06::/64"}' > /etc/docker/daemon.json
systemctl enable --now docker || systemctl start docker

# x86_64 helper containers (Minio/Vault/KMIP) run on this aarch64 worker under
# qemu user-mode emulation via a binfmt_misc handler. The static qemu-x86_64 is
# extracted from a digest-pinned image in Percona's ECR Public mirror, avoiding
# any EPEL dependency on Amazon Linux 2. Magic/mask/flags match the canonical
# qemu-x86_64 registration; the F flag opens the interpreter at registration so
# it is reachable inside container mount namespaces. Bounded so a pull failure
# degrades to no-emulation rather than hanging boot (this script has no errexit).
qemu_img="public.ecr.aws/e7j3v3n0/qemu-binfmt@sha256:d3b963f787999e6c0219a48dba02978769286ff61a5f4d26245cb6a6e5567ea3"
if [ "$(uname -m)" = "aarch64" ] && [ ! -e /proc/sys/fs/binfmt_misc/qemu-x86_64 ]; then
    if [ ! -e /proc/sys/fs/binfmt_misc/register ]; then
        modprobe binfmt_misc || true
        mountpoint -q /proc/sys/fs/binfmt_misc || mount -t binfmt_misc binfmt_misc /proc/sys/fs/binfmt_misc || true
    fi
    aws ecr-public get-login-password --region us-east-1 \
        | docker login --username AWS --password-stdin public.ecr.aws || true
    qemu_pulled=""
    for attempt in 1 2 3 4 5; do
        if docker pull --platform linux/arm64 "${qemu_img}"; then qemu_pulled="ok"; break; fi
        echo "qemu-binfmt pull attempt ${attempt} failed; retrying" >&2
        sleep 5
    done
    if [ "${qemu_pulled}" = "ok" ]; then
        qemu_cid="$(docker create --platform linux/arm64 "${qemu_img}")"
        docker cp "${qemu_cid}:/usr/bin/qemu-x86_64" /usr/local/bin/qemu-x86_64
        docker rm -f "${qemu_cid}" || true
        chmod 0755 /usr/local/bin/qemu-x86_64
        if [ -e /proc/sys/fs/binfmt_misc/register ]; then
            printf '%s\n' ':qemu-x86_64:M::\x7fELF\x02\x01\x01\x00\x00\x00\x00\x00\x00\x00\x00\x00\x02\x00\x3e\x00:\xff\xff\xff\xff\xff\xfe\xfe\xfc\xff\xff\xff\xff\xff\xff\xff\xff\xfe\xff\xff\xff:/usr/local/bin/qemu-x86_64:POF' \
                > /proc/sys/fs/binfmt_misc/register || true
        fi
        [ -e /proc/sys/fs/binfmt_misc/qemu-x86_64 ] || echo "qemu-x86_64 binfmt registration failed" >&2
    else
        echo "qemu-binfmt image unavailable after 5 attempts; x86_64 emulation disabled" >&2
    fi
fi
