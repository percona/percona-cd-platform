# Owner: pmm
#
# AWS resources for the pmm Jenkins agent AMI factory: two GitHub Actions OIDC
# roles (bake, promote), the SSM builder instance profile, and the egress-only
# builder security group in the pmm master VPC. Consumed by
# .github/workflows/agent-ami-factory.yml in percona/pmm, which bakes the two
# pmm-worker-3 agent AMIs with Packer over Session Manager, smoke-tests each
# candidate, and promotes the pair by tag from a job bound to a GitHub
# environment.
#
# Two roles so the environment gate is enforced by IAM, not only by GitHub:
#   bake     trusts the main branch subject, can launch builders, create and
#            tag candidate images, delete candidates and incomplete bakes, never
#            write a live or previous tag
#   promote  trusts only the two environment subjects, can move images between
#            candidate, live and previous inside one family and manage their
#            deprecation dates, cannot launch, delete or tag anything else
#
# Tag contract the policies are built around (iit-billing-tag values):
#   pmm-agent-ami-factory         builder + smoke instances/volumes/key pairs,
#                                 and every freshly created AMI until Packer tags
#                                 it (an AMI still carrying it is an incomplete
#                                 bake and may be deleted by the bake role)
#   pmm-worker-3-candidate        baked, not promoted (deletable by bake)
#   pmm-worker-3                  live pair selected by the Docker Farm cloud and
#                                 the staging pipelines (never deletable here)
#   pmm-worker-3-previous         demoted predecessor, rollback target (never
#                                 deletable here)
#   pmm-worker-3-test*            env=test family, never selected by a consumer,
#                                 deletable by bake, never mixes with prod values
# Live pmm agents carry iit-billing-tag=pmm-worker-3 on their instances, so no
# instance-side statement may condition on that value.
#
# Trust (federated OIDC, StringEquals aud + sub, no wildcards) is owned by
# ./modules/github-oidc-role; this file owns the subject allowlists, the two
# least-privilege policies, the builder profile and the security group.

# ---------------------------------------------------------------------------
# Builder SSM instance profile (Packer builder + smoke instance run with this)
# ---------------------------------------------------------------------------
data "aws_iam_policy_document" "pmm_agent_ami_builder_trust" {
  statement {
    effect  = "Allow"
    actions = ["sts:AssumeRole"]
    principals {
      type        = "Service"
      identifiers = ["ec2.amazonaws.com"]
    }
  }
}

resource "aws_iam_role" "pmm_agent_ami_builder_ssm" {
  name               = "pmm-agent-ami-builder-ssm"
  description        = "Builder + smoke instance profile so Packer connects over SSM Session Manager (no inbound SSH). PKG-1463."
  assume_role_policy = data.aws_iam_policy_document.pmm_agent_ami_builder_trust.json
  tags               = merge(local.tags, { team = "pmm" })
}

resource "aws_iam_role_policy_attachment" "pmm_agent_ami_builder_ssm_core" {
  role       = aws_iam_role.pmm_agent_ami_builder_ssm.name
  policy_arn = "arn:aws:iam::aws:policy/AmazonSSMManagedInstanceCore"
}

resource "aws_iam_instance_profile" "pmm_agent_ami_builder_ssm" {
  name = "pmm-agent-ami-builder-ssm"
  role = aws_iam_role.pmm_agent_ami_builder_ssm.name
  tags = merge(local.tags, { team = "pmm" })
}

# ---------------------------------------------------------------------------
# No-ingress security group for the builder + smoke instance, in the pmm VPC
# (the templates' vpc_filter tag:Name=jenkins-pmm-amzn2). Supplying it to
# Packer (security_group_filter group-name) disables Packer's temporary SG, so
# the bake role needs no SG create/authorize rights.
# ---------------------------------------------------------------------------
resource "aws_security_group" "pmm_agent_ami_builder" {
  provider    = aws.us-east-2
  name        = "pmm-agent-ami-factory-builder"
  description = "Egress-only SG for the pmm agent AMI Packer builder + smoke instance (SSM Session Manager, no inbound)."
  vpc_id      = module.pmm.vpc_id

  egress {
    description = "All egress (SSM endpoints, AlmaLinux and tool downloads, container registries over 443)."
    from_port   = 0
    to_port     = 0
    protocol    = "-1"
    cidr_blocks = ["0.0.0.0/0"]
  }

  # Intentionally NO ingress block: no inbound, SSM-only.
  tags = merge(local.tags, {
    Name              = "pmm-agent-ami-factory-builder"
    "iit-billing-tag" = "pmm-agent-ami-factory"
    team              = "pmm"
  })
}

# ---------------------------------------------------------------------------
# Tag vocabulary shared by both policies
# ---------------------------------------------------------------------------
locals {
  pmm_agent_ami_factory_acct = data.aws_caller_identity.current.account_id
  pmm_agent_ami_factory_reg  = "us-east-2"
  pmm_agent_ami_builder_tag  = "pmm-agent-ami-factory"

  pmm_agent_ami_prod_candidate = "pmm-worker-3-candidate"
  pmm_agent_ami_prod_live      = "pmm-worker-3"
  pmm_agent_ami_prod_previous  = "pmm-worker-3-previous"
  pmm_agent_ami_test_candidate = "pmm-worker-3-test-candidate"
  pmm_agent_ami_test_live      = "pmm-worker-3-test"
  pmm_agent_ami_test_previous  = "pmm-worker-3-test-previous"

  pmm_agent_ami_candidates = [local.pmm_agent_ami_prod_candidate, local.pmm_agent_ami_test_candidate]

  # What the bake role may deregister or delete: incomplete bakes, candidates,
  # the whole test family. Never the prod live pair, never a prod predecessor.
  pmm_agent_ami_deletable = [
    local.pmm_agent_ami_builder_tag,
    local.pmm_agent_ami_prod_candidate,
    local.pmm_agent_ami_test_candidate, local.pmm_agent_ami_test_live, local.pmm_agent_ami_test_previous,
  ]
}

# ---------------------------------------------------------------------------
# Bake role permissions: launch, provision, image, tag as candidate, clean up
# ---------------------------------------------------------------------------
data "aws_iam_policy_document" "gha_pmm_agent_ami_bake_perms" {
  # Launch the builder + smoke instance: region-scoped, and only when the
  # request tags it as a factory instance (Packer run_tags become
  # TagSpecifications at RunInstances).
  statement {
    sid       = "RunInstancesInstanceResource"
    effect    = "Allow"
    actions   = ["ec2:RunInstances"]
    resources = ["arn:aws:ec2:${local.pmm_agent_ami_factory_reg}:${local.pmm_agent_ami_factory_acct}:instance/*"]
    condition {
      test     = "StringEquals"
      variable = "aws:RequestedRegion"
      values   = [local.pmm_agent_ami_factory_reg]
    }
    condition {
      test     = "StringEquals"
      variable = "aws:RequestTag/iit-billing-tag"
      values   = [local.pmm_agent_ami_builder_tag]
    }
  }

  statement {
    sid     = "RunInstancesSupportingResources"
    effect  = "Allow"
    actions = ["ec2:RunInstances"]
    resources = [
      "arn:aws:ec2:${local.pmm_agent_ami_factory_reg}::image/*",
      "arn:aws:ec2:${local.pmm_agent_ami_factory_reg}::snapshot/*",
      "arn:aws:ec2:${local.pmm_agent_ami_factory_reg}:${local.pmm_agent_ami_factory_acct}:network-interface/*",
      "arn:aws:ec2:${local.pmm_agent_ami_factory_reg}:${local.pmm_agent_ami_factory_acct}:subnet/*",
      "arn:aws:ec2:${local.pmm_agent_ami_factory_reg}:${local.pmm_agent_ami_factory_acct}:volume/*",
      "arn:aws:ec2:${local.pmm_agent_ami_factory_reg}:${local.pmm_agent_ami_factory_acct}:key-pair/*",
      aws_security_group.pmm_agent_ami_builder.arn,
    ]
    condition {
      test     = "StringEquals"
      variable = "aws:RequestedRegion"
      values   = [local.pmm_agent_ami_factory_reg]
    }
  }

  # Tags written while creating a resource. Packer stamps run_tags on the
  # temporary key pair (CreateKeyPair), the instance and volumes
  # (RunInstances), and the AMI and its snapshots (CreateImage): factory value
  # only.
  statement {
    sid       = "CreateTagsOnCreate"
    effect    = "Allow"
    actions   = ["ec2:CreateTags"]
    resources = ["*"]
    condition {
      test     = "StringEquals"
      variable = "ec2:CreateAction"
      values   = ["RunInstances", "CreateImage", "CreateSnapshot", "CreateKeyPair"]
    }
    condition {
      test     = "StringEquals"
      variable = "aws:RequestTag/iit-billing-tag"
      values   = [local.pmm_agent_ami_builder_tag]
    }
    condition {
      test     = "StringEquals"
      variable = "aws:RequestedRegion"
      values   = [local.pmm_agent_ami_factory_reg]
    }
  }

  # Packer's own tagging: `tags` go on the image AND its snapshots first (the
  # resources still carry the factory value), then `snapshot_tags` override the
  # snapshots (already candidate). Both land on a candidate value.
  statement {
    sid     = "CreateTagsCandidate"
    effect  = "Allow"
    actions = ["ec2:CreateTags"]
    resources = [
      "arn:aws:ec2:${local.pmm_agent_ami_factory_reg}::image/*",
      "arn:aws:ec2:${local.pmm_agent_ami_factory_reg}::snapshot/*",
    ]
    condition {
      test     = "StringEquals"
      variable = "aws:ResourceTag/iit-billing-tag"
      values   = concat([local.pmm_agent_ami_builder_tag], local.pmm_agent_ami_candidates)
    }
    condition {
      test     = "StringEquals"
      variable = "aws:RequestTag/iit-billing-tag"
      values   = local.pmm_agent_ami_candidates
    }
  }

  # Packer always creates a temporary key pair (packer_<uuid>), even over
  # session_manager.
  statement {
    sid       = "TemporaryKeyPair"
    effect    = "Allow"
    actions   = ["ec2:CreateKeyPair", "ec2:DeleteKeyPair"]
    resources = ["arn:aws:ec2:${local.pmm_agent_ami_factory_reg}:${local.pmm_agent_ami_factory_acct}:key-pair/packer_*"]
    condition {
      test     = "StringEquals"
      variable = "aws:RequestedRegion"
      values   = [local.pmm_agent_ami_factory_reg]
    }
  }

  # Stop/terminate ONLY factory-tagged builder/smoke instances (run_tags apply
  # the factory value at RunInstances, before Packer's Stop/Terminate calls).
  statement {
    sid       = "ManageFactoryInstances"
    effect    = "Allow"
    actions   = ["ec2:StopInstances", "ec2:TerminateInstances"]
    resources = ["arn:aws:ec2:${local.pmm_agent_ami_factory_reg}:${local.pmm_agent_ami_factory_acct}:instance/*"]
    condition {
      test     = "StringEquals"
      variable = "aws:ResourceTag/iit-billing-tag"
      values   = [local.pmm_agent_ami_builder_tag]
    }
  }

  # CreateImage: the instance being imaged must be a factory instance (the
  # action can reboot it); the image and snapshot ARNs are create-time.
  statement {
    sid       = "CreateImageFromFactoryInstance"
    effect    = "Allow"
    actions   = ["ec2:CreateImage"]
    resources = ["arn:aws:ec2:${local.pmm_agent_ami_factory_reg}:${local.pmm_agent_ami_factory_acct}:instance/*"]
    condition {
      test     = "StringEquals"
      variable = "aws:ResourceTag/iit-billing-tag"
      values   = [local.pmm_agent_ami_builder_tag]
    }
  }
  statement {
    sid     = "CreateImageOutputs"
    effect  = "Allow"
    actions = ["ec2:CreateImage"]
    resources = [
      "arn:aws:ec2:${local.pmm_agent_ami_factory_reg}::image/*",
      "arn:aws:ec2:${local.pmm_agent_ami_factory_reg}::snapshot/*",
    ]
    condition {
      test     = "StringEquals"
      variable = "aws:RequestedRegion"
      values   = [local.pmm_agent_ami_factory_reg]
    }
  }

  # Packer's deprecate_at runs before its tagging step, while the AMI still
  # carries the factory value; candidates keep the date until promotion.
  statement {
    sid       = "ImageDeprecationBake"
    effect    = "Allow"
    actions   = ["ec2:EnableImageDeprecation"]
    resources = ["arn:aws:ec2:${local.pmm_agent_ami_factory_reg}::image/*"]
    condition {
      test     = "StringEquals"
      variable = "aws:ResourceTag/iit-billing-tag"
      values   = concat([local.pmm_agent_ami_builder_tag], local.pmm_agent_ami_candidates)
    }
  }

  # Deregister / delete ONLY incomplete bakes, candidates and the test family
  # (Packer's own error cleanup, the janitor, smoke-failed candidates).
  statement {
    sid       = "AmiSnapshotCleanup"
    effect    = "Allow"
    actions   = ["ec2:DeregisterImage", "ec2:DeleteSnapshot"]
    resources = ["*"]
    condition {
      test     = "StringEquals"
      variable = "aws:ResourceTag/iit-billing-tag"
      values   = local.pmm_agent_ami_deletable
    }
    condition {
      test     = "StringEquals"
      variable = "aws:RequestedRegion"
      values   = [local.pmm_agent_ami_factory_reg]
    }
  }

  # Console output of a factory instance: the only diagnostic when the SSM
  # bootstrap in user_data fails and the session never comes up.
  statement {
    sid       = "ConsoleOutputOfFactoryInstance"
    effect    = "Allow"
    actions   = ["ec2:GetConsoleOutput"]
    resources = ["arn:aws:ec2:${local.pmm_agent_ami_factory_reg}:${local.pmm_agent_ami_factory_acct}:instance/*"]
    condition {
      test     = "StringEquals"
      variable = "aws:ResourceTag/iit-billing-tag"
      values   = [local.pmm_agent_ami_builder_tag]
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
      "ec2:DescribeImageAttribute",
    ]
    resources = ["*"]
  }

  # SSM Session Manager tunnel (Packer ssh_interface=session_manager uses the
  # AWS-StartPortForwardingSession document). Instance target is factory-tag
  # scoped; the AWS-owned documents cannot carry a resourceTag condition.
  # Session housekeeping is scoped to this role's own sessions, whose ids start
  # with the role session name the workflow sets.
  statement {
    sid       = "SsmStartSessionOnFactoryInstance"
    effect    = "Allow"
    actions   = ["ssm:StartSession"]
    resources = ["arn:aws:ec2:${local.pmm_agent_ami_factory_reg}:${local.pmm_agent_ami_factory_acct}:instance/*"]
    condition {
      test     = "StringEquals"
      variable = "ssm:resourceTag/iit-billing-tag"
      values   = [local.pmm_agent_ami_builder_tag]
    }
  }
  statement {
    sid     = "SsmStartSessionDocument"
    effect  = "Allow"
    actions = ["ssm:StartSession"]
    resources = [
      "arn:aws:ssm:${local.pmm_agent_ami_factory_reg}::document/AWS-StartPortForwardingSession",
      "arn:aws:ssm:${local.pmm_agent_ami_factory_reg}::document/AWS-StartSSHSession",
    ]
  }
  statement {
    sid       = "SsmOwnSessions"
    effect    = "Allow"
    actions   = ["ssm:TerminateSession", "ssm:ResumeSession"]
    resources = ["arn:aws:ssm:${local.pmm_agent_ami_factory_reg}:${local.pmm_agent_ami_factory_acct}:session/pmm-agent-ami-factory-*"]
  }
  statement {
    sid       = "SsmDescribeInstanceInformation"
    effect    = "Allow"
    actions   = ["ssm:DescribeInstanceInformation"]
    resources = ["*"]
  }

  # Pass ONLY the builder SSM role to EC2.
  statement {
    sid       = "PassBuilderRoleToEc2"
    effect    = "Allow"
    actions   = ["iam:PassRole"]
    resources = [aws_iam_role.pmm_agent_ami_builder_ssm.arn]
    condition {
      test     = "StringEquals"
      variable = "iam:PassedToService"
      values   = ["ec2.amazonaws.com"]
    }
  }
}

module "pmm_agent_ami_bake_oidc" {
  source = "./modules/github-oidc-role"

  name                 = "gha-pmm-agent-ami-bake"
  role_name_prefix     = "${local.cluster_name}-"
  description          = "Assumed by the bake jobs of the pmm agent AMI factory workflow in percona/pmm (main) via OIDC: launch builders over SSM, create and tag candidate AMIs, delete candidates. Cannot promote. PKG-1463."
  max_session_duration = 7200 # a bake plus smoke can exceed the 1 h STS default

  subject_claims          = var.pmm_agent_ami_bake_subject_claims
  permissions_policy_json = data.aws_iam_policy_document.gha_pmm_agent_ami_bake_perms.json
  tags                    = merge(local.tags, { team = "pmm" })
}

# ---------------------------------------------------------------------------
# Promote role permissions: move images between candidate, live and previous
# inside one family, manage their deprecation dates, nothing else
# ---------------------------------------------------------------------------
data "aws_iam_policy_document" "gha_pmm_agent_ami_promote_perms" {
  # Prod family: candidate -> live (promotion), live -> previous (demotion),
  # previous -> live (rollback), same-state rewrites (retries, snapshot
  # reconciliation). No transition to a candidate or test value exists.
  statement {
    sid     = "CreateTagsProdFamily"
    effect  = "Allow"
    actions = ["ec2:CreateTags"]
    resources = [
      "arn:aws:ec2:${local.pmm_agent_ami_factory_reg}::image/*",
      "arn:aws:ec2:${local.pmm_agent_ami_factory_reg}::snapshot/*",
    ]
    condition {
      test     = "StringEquals"
      variable = "aws:ResourceTag/iit-billing-tag"
      values   = [local.pmm_agent_ami_prod_candidate, local.pmm_agent_ami_prod_live, local.pmm_agent_ami_prod_previous]
    }
    condition {
      test     = "StringEquals"
      variable = "aws:RequestTag/iit-billing-tag"
      values   = [local.pmm_agent_ami_prod_live, local.pmm_agent_ami_prod_previous]
    }
  }

  # Test family, same transitions, never touching a prod value.
  statement {
    sid     = "CreateTagsTestFamily"
    effect  = "Allow"
    actions = ["ec2:CreateTags"]
    resources = [
      "arn:aws:ec2:${local.pmm_agent_ami_factory_reg}::image/*",
      "arn:aws:ec2:${local.pmm_agent_ami_factory_reg}::snapshot/*",
    ]
    condition {
      test     = "StringEquals"
      variable = "aws:ResourceTag/iit-billing-tag"
      values   = [local.pmm_agent_ami_test_candidate, local.pmm_agent_ami_test_live, local.pmm_agent_ami_test_previous]
    }
    condition {
      test     = "StringEquals"
      variable = "aws:RequestTag/iit-billing-tag"
      values   = [local.pmm_agent_ami_test_live, local.pmm_agent_ami_test_previous]
    }
  }

  # Clear the deprecation date on a promoted image, set one on a demoted image.
  statement {
    sid       = "ImageDeprecationPromote"
    effect    = "Allow"
    actions   = ["ec2:EnableImageDeprecation", "ec2:DisableImageDeprecation"]
    resources = ["arn:aws:ec2:${local.pmm_agent_ami_factory_reg}::image/*"]
    condition {
      test     = "StringEquals"
      variable = "aws:ResourceTag/iit-billing-tag"
      values = [
        local.pmm_agent_ami_prod_candidate, local.pmm_agent_ami_prod_live, local.pmm_agent_ami_prod_previous,
        local.pmm_agent_ami_test_candidate, local.pmm_agent_ami_test_live, local.pmm_agent_ami_test_previous,
      ]
    }
  }

  statement {
    sid       = "DescribeImagesReadOnly"
    effect    = "Allow"
    actions   = ["ec2:DescribeImages", "ec2:DescribeSnapshots", "ec2:DescribeTags", "ec2:DescribeImageAttribute"]
    resources = ["*"]
  }
}

module "pmm_agent_ami_promote_oidc" {
  source = "./modules/github-oidc-role"

  name             = "gha-pmm-agent-ami-promote"
  role_name_prefix = "${local.cluster_name}-"
  description      = "Assumed only by the environment-gated promote job of the pmm agent AMI factory workflow in percona/pmm via OIDC: retag candidate, live and previous images and manage their deprecation. Cannot launch or delete. PKG-1463."

  subject_claims          = var.pmm_agent_ami_promote_subject_claims
  permissions_policy_json = data.aws_iam_policy_document.gha_pmm_agent_ami_promote_perms.json
  tags                    = merge(local.tags, { team = "pmm" })
}

output "pmm_agent_ami_bake_oidc_role_arn" {
  description = "role-to-assume for the bake jobs of the pmm agent AMI factory workflow (the workflow default, or repo secret PMM_AGENT_AMI_BAKE_ROLE_ARN)."
  value       = module.pmm_agent_ami_bake_oidc.role_arn
}

output "pmm_agent_ami_promote_oidc_role_arn" {
  description = "role-to-assume for the environment-gated promote job of the pmm agent AMI factory workflow (the workflow default, or repo secret PMM_AGENT_AMI_PROMOTE_ROLE_ARN)."
  value       = module.pmm_agent_ami_promote_oidc.role_arn
}
