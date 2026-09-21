# Owner: platform
# Shared PINNED Amazon Linux 2023 AMI sources (x86_64 minimal + arm64).
#
# One release string drives the whole fleet: every Jenkins master and every
# Graviton worker fleet resolves the SAME AL2023 build, in its own region.
# The lookup is an EXACT name match, so a new AWS release changes nothing
# until `al2023_release` below is bumped deliberately.
#
# Why pinned and not "latest": the previous sources resolved "latest" at PLAN
# time (an `ami-amazon-linux-latest` SSM parameter for the masters,
# `most_recent = true` for the arm fleets). Every AWS AL2023 release therefore
# rewrote image_id on ~17 launch templates and bumped the ASGs behind them, so
# an unrelated one-line change planned as a 27-resource diff and a fleet-wide
# worker image roll could ride along unnoticed. Two worker incidents followed
# such a roll in July. Pinning makes the image a reviewed, one-line change with
# its own plan, and keeps every other plan honest.
#
# To bump: change `al2023_release`, plan, and treat the worker roll as a change
# needing a canary, not a routine apply.
#
# Minimal (not standard) AL2023 for the masters: the module user-data
# yum-installs everything the master needs (java-17, jenkins, git, aws-cli,
# xfsprogs, jq, ...) on top of a bare AL2023, so the smaller minimal image is
# sufficient and is the long-standing master AMI choice. The arm64 workers use
# the standard image; AL2 is past end of support and its kernel 4.14 lacks
# openat2, which current Ubuntu 26.04 containers require.
#
# No churn on apply: the jenkins-master module's on-demand aws_instance has
# lifecycle.ignore_changes = [ami], and the spot path uses $Latest on the launch
# template, so a changed value only bumps the launch-template version. The new
# AMI is consumed on the NEXT instance replacement, never on a plain apply.
#
# AMI ids are region-local, so each data block runs in the consuming master's
# own region via the matching provider alias. Add a region entry when a master
# in a new region migrates in.
#
# owners is pinned per the provider's supply-chain guidance. The name is exact,
# so each lookup matches exactly one image and errors loudly if AWS ever
# withdraws that build.

locals {
  al2023_release = "2023.12.20260909.0"

  al2023_minimal_name = "al2023-ami-minimal-${local.al2023_release}-kernel-6.1-x86_64"
  al2023_arm64_name   = "al2023-ami-${local.al2023_release}-kernel-6.1-arm64"
}

# ---------- x86_64 minimal: the Jenkins masters ----------

data "aws_ami" "al2023_minimal_usw2" { # us-west-2: ps80, pxb, psmdb
  provider = aws.us-west-2
  owners   = ["amazon"]

  filter {
    name   = "name"
    values = [local.al2023_minimal_name]
  }
}

data "aws_ami" "al2023_minimal_euw1" { # eu-west-1: rel, cloud
  provider = aws.eu-west-1
  owners   = ["amazon"]

  filter {
    name   = "name"
    values = [local.al2023_minimal_name]
  }
}

data "aws_ami" "al2023_minimal_use2" { # us-east-2: pmm
  provider = aws.us-east-2
  owners   = ["amazon"]

  filter {
    name   = "name"
    values = [local.al2023_minimal_name]
  }
}

data "aws_ami" "al2023_minimal_euc1" { # eu-central-1: ps57, pg
  provider = aws.eu-central-1
  owners   = ["amazon"]

  filter {
    name   = "name"
    values = [local.al2023_minimal_name]
  }
}

data "aws_ami" "al2023_minimal_usw1" { # us-west-1: pxc
  provider = aws.us-west-1
  owners   = ["amazon"]

  filter {
    name   = "name"
    values = [local.al2023_minimal_name]
  }
}

# ---------- arm64 standard: the Graviton worker fleets ----------
#
# Passed as the modules' `ami_id`, which switches off their own
# `most_recent = true` lookup (count = var.ami_id == null ? 1 : 0).

data "aws_ami" "al2023_arm64_usw2" { # us-west-2: ps80, psmdb, pxb
  provider = aws.us-west-2
  owners   = ["amazon"]

  filter {
    name   = "name"
    values = [local.al2023_arm64_name]
  }
}

data "aws_ami" "al2023_arm64_euw1" { # eu-west-1: rel, cloud, ps3
  provider = aws.eu-west-1
  owners   = ["amazon"]

  filter {
    name   = "name"
    values = [local.al2023_arm64_name]
  }
}

data "aws_ami" "al2023_arm64_use2" { # us-east-2: pmm
  provider = aws.us-east-2
  owners   = ["amazon"]

  filter {
    name   = "name"
    values = [local.al2023_arm64_name]
  }
}

data "aws_ami" "al2023_arm64_euc1" { # eu-central-1: ps57, pg
  provider = aws.eu-central-1
  owners   = ["amazon"]

  filter {
    name   = "name"
    values = [local.al2023_arm64_name]
  }
}

data "aws_ami" "al2023_arm64_usw1" { # us-west-1: pxc
  provider = aws.us-west-1
  owners   = ["amazon"]

  filter {
    name   = "name"
    values = [local.al2023_arm64_name]
  }
}
