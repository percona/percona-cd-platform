# Owner: platform
# Shared "always-latest AL2023 minimal" AMI sources.
#
# AWS publishes the current Amazon Linux 2023 minimal AMI id as a public SSM
# parameter, per region. Reading it here (instead of hand-pinning an `ami-...`
# literal in each master file) means every master resolves the SAME latest
# AL2023 minimal, and a master REPLACEMENT always lands on the current release.
#
# No churn on apply: the jenkins-master module's on-demand aws_instance has
# lifecycle.ignore_changes = [ami], and the spot path uses $Latest on the launch
# template, so a changed value only bumps the launch-template version. The new
# AMI is consumed on the NEXT instance replacement, never on a plain apply.
#
# Minimal (not standard) AL2023: the module user-data yum-installs everything the
# master needs (java-17, jenkins, git, aws-cli, xfsprogs, jq, ...) on top of a
# bare AL2023, so the smaller minimal image is sufficient and is the long-standing
# master AMI choice.
#
# The SSM parameter is region-local (it resolves to a different ami- per region),
# so each data block must run in the master's own region via the matching
# provider alias. Add a region entry when a master in a new region migrates in.

locals {
  al2023_minimal_param = "/aws/service/ami-amazon-linux-latest/al2023-ami-minimal-kernel-6.1-x86_64"
}

data "aws_ssm_parameter" "al2023_minimal_usw2" { # us-west-2: ps80, pxb
  provider = aws.us-west-2
  name     = local.al2023_minimal_param
}

data "aws_ssm_parameter" "al2023_minimal_euw1" { # eu-west-1: ps3
  provider = aws.eu-west-1
  name     = local.al2023_minimal_param
}

data "aws_ssm_parameter" "al2023_minimal_euc1" { # eu-central-1: ps57
  provider = aws.eu-central-1
  name     = local.al2023_minimal_param
}

data "aws_ssm_parameter" "al2023_minimal_usw1" { # us-west-1: pxc
  provider = aws.us-west-1
  name     = local.al2023_minimal_param
}
