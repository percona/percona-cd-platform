# Copyright (C) 2026 Percona LLC
#
# AWS resources for the Oracle Linux package-test AMI factory: the GitHub Actions
# OIDC role, the SSM builder instance profile, and the egress-only builder security
# group. Consumed by the factory workflow in Percona-Lab/jenkins-pipelines.
#
# The factory's GHA workflow assumes the OIDC role (no static keys), launches a
# Packer builder, and connects over AWS Session Manager (no inbound SSH). The
# builder + the smoke-test instance run with the SSM instance profile defined
# here so they register as managed nodes.
#
# Trust shape (federated OIDC, StringEquals aud + sub, no wildcards) is owned by
# ./modules/github-oidc-role; this file owns the subject allowlist, the
# least-privilege EC2/AMI/SSM policy, and the builder instance profile.

# ---------------------------------------------------------------------------
# Builder SSM instance profile (Packer builder + smoke instance run with this)
# ---------------------------------------------------------------------------
data "aws_iam_policy_document" "ppg_ami_builder_trust" {
  statement {
    effect  = "Allow"
    actions = ["sts:AssumeRole"]
    principals {
      type        = "Service"
      identifiers = ["ec2.amazonaws.com"]
    }
  }
}

resource "aws_iam_role" "ppg_ami_builder_ssm" {
  name               = "ppg-ami-builder-ssm"
  description        = "Builder + smoke instance profile so Packer/smoke connect over SSM Session Manager (no inbound SSH)."
  assume_role_policy = data.aws_iam_policy_document.ppg_ami_builder_trust.json
  tags               = local.tags
}

resource "aws_iam_role_policy_attachment" "ppg_ami_builder_ssm_core" {
  role       = aws_iam_role.ppg_ami_builder_ssm.name
  policy_arn = "arn:aws:iam::aws:policy/AmazonSSMManagedInstanceCore"
}

resource "aws_iam_instance_profile" "ppg_ami_builder_ssm" {
  name = "ppg-ami-builder-ssm"
  role = aws_iam_role.ppg_ami_builder_ssm.name
  tags = local.tags
}

# ---------------------------------------------------------------------------
# No-ingress security group for the Packer builder + smoke instance.
# Session Manager needs no inbound rule; supplying this SG to Packer
# (security_group_filter group-name) disables Packer's temporary SG, so the
# OIDC role drops Create/DeleteSecurityGroup + Authorize/Revoke entirely.
# ---------------------------------------------------------------------------
data "aws_vpc" "ppg_ami_factory_default" {
  provider = aws.eu-central-1
  default  = true
}

resource "aws_security_group" "ppg_ami_builder" {
  provider    = aws.eu-central-1
  name        = "ppg-ami-factory-builder"
  description = "Egress-only SG for the Packer OL AMI builder + smoke instance (SSM Session Manager, no inbound)."
  vpc_id      = data.aws_vpc.ppg_ami_factory_default.id

  egress {
    description = "All egress (SSM endpoints + Oracle/Percona yum repos over 443)."
    from_port   = 0
    to_port     = 0
    protocol    = "-1"
    cidr_blocks = ["0.0.0.0/0"]
  }

  # Intentionally NO ingress block: no inbound, SSM-only.
  tags = merge(local.tags, {
    Name              = "ppg-ami-factory-builder"
    "iit-billing-tag" = "ppg-ami-factory"
  })
}

# ---------------------------------------------------------------------------
# Least-privilege permissions for the GHA OIDC factory role
# ---------------------------------------------------------------------------
locals {
  ppg_ami_factory_acct = data.aws_caller_identity.current.account_id
  ppg_ami_factory_reg  = var.ppg_ami_factory_region
}

data "aws_iam_policy_document" "gha_ppg_ami_factory_perms" {
  # Launch the builder + smoke instance (region + billing-tag scoped).
  statement {
    sid       = "RunInstancesInstanceResource"
    effect    = "Allow"
    actions   = ["ec2:RunInstances"]
    resources = ["arn:aws:ec2:${local.ppg_ami_factory_reg}:${local.ppg_ami_factory_acct}:instance/*"]
    condition {
      test     = "StringEquals"
      variable = "aws:RequestedRegion"
      values   = [local.ppg_ami_factory_reg]
    }
    condition {
      test     = "StringEquals"
      variable = "aws:RequestTag/iit-billing-tag"
      values   = ["ppg-ami-factory"]
    }
  }

  statement {
    sid     = "RunInstancesSupportingResources"
    effect  = "Allow"
    actions = ["ec2:RunInstances"]
    resources = [
      "arn:aws:ec2:${local.ppg_ami_factory_reg}::image/*",
      "arn:aws:ec2:${local.ppg_ami_factory_reg}::snapshot/*",
      "arn:aws:ec2:${local.ppg_ami_factory_reg}:${local.ppg_ami_factory_acct}:network-interface/*",
      "arn:aws:ec2:${local.ppg_ami_factory_reg}:${local.ppg_ami_factory_acct}:subnet/*",
      "arn:aws:ec2:${local.ppg_ami_factory_reg}:${local.ppg_ami_factory_acct}:security-group/*",
      "arn:aws:ec2:${local.ppg_ami_factory_reg}:${local.ppg_ami_factory_acct}:volume/*",
      "arn:aws:ec2:${local.ppg_ami_factory_reg}:${local.ppg_ami_factory_acct}:key-pair/*",
    ]
    condition {
      test     = "StringEquals"
      variable = "aws:RequestedRegion"
      values   = [local.ppg_ami_factory_reg]
    }
  }

  # CreateTags stays UNCONDITIONED (NOT conditioned on ec2:CreateAction): Packer
  # tags the AMI + snapshots via a separate post-CreateImage CreateTags call that
  # carries no CreateAction key, and the smoke shell-local re-tags the candidate
  # to promote it; an ec2:CreateAction condition would deny both and leave the AMI
  # untagged (which would also break the ResourceTag-gated deletes below).
  # KeyPair create/delete is mandatory: Packer always creates a temp keypair, even
  # over session_manager. SG create/authorize is GONE (pre-created no-ingress SG).
  statement {
    sid    = "TagAndKeypair"
    effect = "Allow"
    actions = [
      "ec2:CreateTags", "ec2:DeleteTags",
      "ec2:CreateKeyPair", "ec2:DeleteKeyPair",
    ]
    resources = ["*"]
    condition {
      test     = "StringEquals"
      variable = "aws:RequestedRegion"
      values   = [local.ppg_ami_factory_reg]
    }
  }

  # Stop/terminate ONLY the factory's own tagged builder/smoke instances
  # (run_tags apply iit-billing-tag at RunInstances create-time, so the tag is
  # present before Packer's Stop/Terminate calls).
  statement {
    sid       = "TerminateTaggedInstances"
    effect    = "Allow"
    actions   = ["ec2:StopInstances", "ec2:TerminateInstances"]
    resources = ["arn:aws:ec2:${local.ppg_ami_factory_reg}:${local.ppg_ami_factory_acct}:instance/*"]
    condition {
      test     = "StringEquals"
      variable = "aws:ResourceTag/iit-billing-tag"
      values   = ["ppg-ami-factory"]
    }
  }

  # AMI/snapshot creation + deprecation. Region-scoped, NOT ResourceTag-scoped:
  # CreateImage/CreateSnapshot are create-time, and EnableImageDeprecation runs
  # BEFORE Packer tags the AMI (StepEnableDeprecation precedes StepCreateTags), so
  # a ResourceTag condition would deny it. RegisterImage / ModifyImageAttribute /
  # DisableImageDeprecation are unused by the refresh bake (dropped); the OL10
  # bootstrap register-image runs outside this role (admin/manual).
  statement {
    sid       = "AmiBakeCreate"
    effect    = "Allow"
    actions   = ["ec2:CreateImage", "ec2:CreateSnapshot", "ec2:EnableImageDeprecation"]
    resources = ["*"]
    condition {
      test     = "StringEquals"
      variable = "aws:RequestedRegion"
      values   = [local.ppg_ami_factory_reg]
    }
  }

  # Deregister / delete ONLY factory-tagged AMIs + snapshots (smoke-fail cleanup
  # and --delete-associated-snapshots). The AMI + snapshot carry iit-billing-tag
  # via the templates' tags/snapshot_tags, so the ResourceTag condition matches.
  statement {
    sid       = "AmiSnapshotCleanup"
    effect    = "Allow"
    actions   = ["ec2:DeregisterImage", "ec2:DeleteSnapshot"]
    resources = ["*"]
    condition {
      test     = "StringEquals"
      variable = "aws:ResourceTag/iit-billing-tag"
      values   = ["ppg-ami-factory"]
    }
    condition {
      test     = "StringEquals"
      variable = "aws:RequestedRegion"
      values   = [local.ppg_ami_factory_reg]
    }
  }

  statement {
    sid    = "DescribeReadOnly"
    effect = "Allow"
    actions = [
      "ec2:DescribeInstances", "ec2:DescribeInstanceStatus", "ec2:DescribeImages",
      "ec2:DescribeSnapshots", "ec2:DescribeVolumes", "ec2:DescribeTags",
      "ec2:DescribeSubnets", "ec2:DescribeVpcs", "ec2:DescribeSecurityGroups",
      "ec2:DescribeRegions", "ec2:DescribeInstanceTypes", "ec2:DescribeKeyPairs",
    ]
    resources = ["*"]
  }

  # SSM Session Manager tunnel. Packer's ssh_interface=session_manager uses the
  # AWS-StartPortForwardingSession document (hardcoded in packer-plugin-amazon).
  # StartSession is split: the instance target is tag-scoped; the AWS-owned
  # session documents are a separate statement (an AWS-managed document cannot
  # carry a resourceTag condition, so combining them would deny document access).
  # SendCommand/GetCommandInvocation/ListCommandInvocations are GONE: the native
  # smoke connects over StartSession only (no send-command).
  statement {
    sid       = "SsmStartSessionOnTaggedInstance"
    effect    = "Allow"
    actions   = ["ssm:StartSession"]
    resources = ["arn:aws:ec2:${local.ppg_ami_factory_reg}:${local.ppg_ami_factory_acct}:instance/*"]
    condition {
      test     = "StringEquals"
      variable = "ssm:resourceTag/iit-billing-tag"
      values   = ["ppg-ami-factory"]
    }
  }
  statement {
    sid     = "SsmStartSessionDocument"
    effect  = "Allow"
    actions = ["ssm:StartSession"]
    resources = [
      "arn:aws:ssm:${local.ppg_ami_factory_reg}::document/AWS-StartPortForwardingSession",
      "arn:aws:ssm:${local.ppg_ami_factory_reg}::document/AWS-StartSSHSession",
    ]
  }
  statement {
    sid       = "SsmSessionHousekeeping"
    effect    = "Allow"
    actions   = ["ssm:TerminateSession", "ssm:ResumeSession", "ssm:DescribeInstanceInformation"]
    resources = ["*"]
  }

  # Pass ONLY the builder SSM role to EC2 (the single PassRole exception).
  statement {
    sid       = "PassBuilderRoleToEc2"
    effect    = "Allow"
    actions   = ["iam:PassRole"]
    resources = [aws_iam_role.ppg_ami_builder_ssm.arn]
    condition {
      test     = "StringEquals"
      variable = "iam:PassedToService"
      values   = ["ec2.amazonaws.com"]
    }
  }
}

module "ppg_ami_factory_oidc" {
  source = "./modules/github-oidc-role"

  name             = "gha-ppg-ami-factory"
  role_name_prefix = "${local.cluster_name}-"
  description      = "Assumed by the PPG Oracle Linux AMI-factory GHA workflow in Percona-Lab/jenkins-pipelines (master) via OIDC to bake OL8/9/10 test-target AMIs over SSM. No static keys."

  subject_claims          = var.ppg_ami_factory_subject_claims
  permissions_policy_json = data.aws_iam_policy_document.gha_ppg_ami_factory_perms.json
  tags                    = local.tags
}

output "ppg_ami_factory_oidc_role_arn" {
  description = "role-to-assume for the PPG AMI-factory GHA workflow (store as repo secret PPG_AMI_FACTORY_ROLE_ARN)."
  value       = module.ppg_ami_factory_oidc.role_arn
}
