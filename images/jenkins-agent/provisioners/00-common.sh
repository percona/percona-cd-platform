#!/bin/bash
# Fleet-common agent payload: everything every worker label family installs at
# boot today (the 2.5-8 min per-launch setup this factory removes). Baked once
# per (os, arch); instance-shape-specific steps (ephemeral-disk mkfs/mount) and
# env-specific ones (/etc/hosts entries, Jenkins remoting) stay in the
# now-minimal boot initScript.
set -euo pipefail

sudo dnf -y -q update

# file(1) is load-bearing for 10-qemu-binfmt.sh's BuildID verification.
sudo dnf -y -q install \
  java-17-amazon-corretto-headless \
  git \
  docker \
  awscli-2 \
  file \
  tar \
  unzip \
  p7zip

sudo systemctl enable docker
sudo systemctl start docker

# Workers run builds as ec2-user via the Jenkins agent: docker group membership
# replaces the per-boot usermod the initScript does today.
sudo usermod -aG docker ec2-user

java -version 2>&1 | head -1
git --version
docker --version
aws --version
