# Owner: platform
#
# AWS resources for the Jenkins agent-image factory: the GitHub Actions OIDC
# role, the SSM builder instance profile, and the egress-only builder security
# group. Consumed by .github/workflows/jenkins-agent-image-factory.yml in THIS
# repository, which bakes the Tier-A worker agent AMIs
# (jenkins-agent-<os>-<arch>) with Packer over SSM Session Manager.
#
# A parameterized sibling of iam-gha-ppg-ami-factory.tf: same trust shape
# (federated OIDC, StringEquals aud + sub via ./modules/github-oidc-role, no
# wildcards), same no-static-keys, no-inbound-SSH builder posture, its own
# subject (this repo), billing tag, and region.

# ---------------------------------------------------------------------------
# Builder SSM instance profile (Packer builder + smoke instance run with this)
# ---------------------------------------------------------------------------
data "aws_iam_policy_document" "agent_image_builder_trust" {
  statement {
    effect  = "Allow"
    actions = ["sts:AssumeRole"]
    principals {
      type        = "Service"
      identifiers = ["ec2.amazonaws.com"]
    }
  }
}

resource "aws_iam_role" "agent_image_builder_ssm" {
  name               = "jenkins-agent-builder-ssm"
  description        = "Builder + smoke instance profile so Packer/smoke connect over SSM Session Manager (no inbound SSH)."
  assume_role_policy = data.aws_iam_policy_document.agent_image_builder_trust.json
  tags               = local.tags
}

resource "aws_iam_role_policy_attachment" "agent_image_builder_ssm_core" {
  role       = aws_iam_role.agent_image_builder_ssm.name
  policy_arn = "arn:aws:iam::aws:policy/AmazonSSMManagedInstanceCore"
}

resource "aws_iam_instance_profile" "agent_image_builder_ssm" {
  name = "jenkins-agent-builder-ssm"
  role = aws_iam_role.agent_image_builder_ssm.name
  tags = local.tags
}

# ---------------------------------------------------------------------------
# No-ingress security group for the Packer builder + smoke instance. Session
# Manager needs no inbound rule; supplying this SG to Packer disables its
# temporary SG, so the OIDC role carries no SG mutations at all.
# ---------------------------------------------------------------------------
data "aws_vpc" "agent_image_factory_default" {
  provider = aws.eu-central-1
  default  = true
}

resource "aws_security_group" "agent_image_builder" {
  provider    = aws.eu-central-1
  name        = "jenkins-agent-factory-builder"
  description = "Egress-only SG for the Packer agent-AMI builder + smoke instance (SSM Session Manager, no inbound)."
  vpc_id      = data.aws_vpc.agent_image_factory_default.id

  egress {
    description = "All egress (SSM endpoints, AL repos, ECR Public over 443)."
    from_port   = 0
    to_port     = 0
    protocol    = "-1"
    cidr_blocks = ["0.0.0.0/0"]
  }

  # Intentionally NO ingress block: no inbound, SSM-only.
  tags = merge(local.tags, {
    Name              = "jenkins-agent-factory-builder"
    "iit-billing-tag" = "jenkins-agent-factory"
  })
}

# ---------------------------------------------------------------------------
# Least-privilege permissions for the GHA OIDC factory role. Statement shapes
# and their rationale mirror iam-gha-ppg-ami-factory.tf (unconditioned
# CreateTags, split SSM statements, single PassRole); see the comments there.
# ---------------------------------------------------------------------------
locals {
  agent_image_factory_acct = data.aws_caller_identity.current.account_id
  agent_image_factory_reg  = var.agent_image_factory_region
}

data "aws_iam_policy_document" "gha_agent_image_factory_perms" {
  statement {
    sid       = "RunInstancesInstanceResource"
    effect    = "Allow"
    actions   = ["ec2:RunInstances"]
    resources = ["arn:aws:ec2:${local.agent_image_factory_reg}:${local.agent_image_factory_acct}:instance/*"]
    condition {
      test     = "StringEquals"
      variable = "aws:RequestedRegion"
      values   = [local.agent_image_factory_reg]
    }
    condition {
      test     = "StringEquals"
      variable = "aws:RequestTag/iit-billing-tag"
      values   = ["jenkins-agent-factory"]
    }
  }

  statement {
    sid     = "RunInstancesSupportingResources"
    effect  = "Allow"
    actions = ["ec2:RunInstances"]
    resources = [
      "arn:aws:ec2:${local.agent_image_factory_reg}::image/*",
      "arn:aws:ec2:${local.agent_image_factory_reg}::snapshot/*",
      "arn:aws:ec2:${local.agent_image_factory_reg}:${local.agent_image_factory_acct}:network-interface/*",
      "arn:aws:ec2:${local.agent_image_factory_reg}:${local.agent_image_factory_acct}:subnet/*",
      "arn:aws:ec2:${local.agent_image_factory_reg}:${local.agent_image_factory_acct}:security-group/*",
      "arn:aws:ec2:${local.agent_image_factory_reg}:${local.agent_image_factory_acct}:volume/*",
      "arn:aws:ec2:${local.agent_image_factory_reg}:${local.agent_image_factory_acct}:key-pair/*",
    ]
    condition {
      test     = "StringEquals"
      variable = "aws:RequestedRegion"
      values   = [local.agent_image_factory_reg]
    }
  }

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
      values   = [local.agent_image_factory_reg]
    }
  }

  statement {
    sid       = "TerminateTaggedInstances"
    effect    = "Allow"
    actions   = ["ec2:StopInstances", "ec2:TerminateInstances"]
    resources = ["arn:aws:ec2:${local.agent_image_factory_reg}:${local.agent_image_factory_acct}:instance/*"]
    condition {
      test     = "StringEquals"
      variable = "aws:ResourceTag/iit-billing-tag"
      values   = ["jenkins-agent-factory"]
    }
  }

  statement {
    sid       = "AmiBakeCreate"
    effect    = "Allow"
    actions   = ["ec2:CreateImage", "ec2:CreateSnapshot", "ec2:EnableImageDeprecation"]
    resources = ["*"]
    condition {
      test     = "StringEquals"
      variable = "aws:RequestedRegion"
      values   = [local.agent_image_factory_reg]
    }
  }

  statement {
    sid       = "AmiSnapshotCleanup"
    effect    = "Allow"
    actions   = ["ec2:DeregisterImage", "ec2:DeleteSnapshot"]
    resources = ["*"]
    condition {
      test     = "StringEquals"
      variable = "aws:ResourceTag/iit-billing-tag"
      values   = ["jenkins-agent-factory"]
    }
    condition {
      test     = "StringEquals"
      variable = "aws:RequestedRegion"
      values   = [local.agent_image_factory_reg]
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

  statement {
    sid       = "SsmStartSessionOnTaggedInstance"
    effect    = "Allow"
    actions   = ["ssm:StartSession"]
    resources = ["arn:aws:ec2:${local.agent_image_factory_reg}:${local.agent_image_factory_acct}:instance/*"]
    condition {
      test     = "StringEquals"
      variable = "ssm:resourceTag/iit-billing-tag"
      values   = ["jenkins-agent-factory"]
    }
  }
  statement {
    sid     = "SsmStartSessionDocument"
    effect  = "Allow"
    actions = ["ssm:StartSession"]
    resources = [
      "arn:aws:ssm:${local.agent_image_factory_reg}::document/AWS-StartPortForwardingSession",
      "arn:aws:ssm:${local.agent_image_factory_reg}::document/AWS-StartSSHSession",
    ]
  }
  statement {
    sid       = "SsmSessionHousekeeping"
    effect    = "Allow"
    actions   = ["ssm:TerminateSession", "ssm:ResumeSession", "ssm:DescribeInstanceInformation"]
    resources = ["*"]
  }

  statement {
    sid       = "PassBuilderRoleToEc2"
    effect    = "Allow"
    actions   = ["iam:PassRole"]
    resources = [aws_iam_role.agent_image_builder_ssm.arn]
    condition {
      test     = "StringEquals"
      variable = "iam:PassedToService"
      values   = ["ec2.amazonaws.com"]
    }
  }
}

module "agent_image_factory_oidc" {
  source = "./modules/github-oidc-role"

  name             = "gha-agent-image-factory"
  role_name_prefix = "${local.cluster_name}-"
  description      = "Assumed by the jenkins-agent image-factory GHA workflow in Percona/percona-cd-platform (main) via OIDC to bake worker agent AMIs over SSM. No static keys."

  subject_claims          = var.agent_image_factory_subject_claims
  permissions_policy_json = data.aws_iam_policy_document.gha_agent_image_factory_perms.json
  tags                    = local.tags
}

output "agent_image_factory_oidc_role_arn" {
  description = "role-to-assume for the agent image-factory GHA workflow (store as repo secret AGENT_FACTORY_ROLE_ARN)."
  value       = module.agent_image_factory_oidc.role_arn
}
