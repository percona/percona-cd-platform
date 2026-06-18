# Owner: platform
# Terraform-managed DLM snapshot policies for the EC2 Jenkins master DATA
# volumes, one per master region. Replaces the hand-made console policies with
# reviewable IaC: daily snapshots, 14 retained, cross-region copy to the cluster
# region. Selection is the single tag snapshot-policy=jenkins-master, set on each
# master data volume in modules/jenkins-master. The cadence and the cross-region
# DR copy are the explicit RPO floor in docs/adr/0037-master-ebs-snapshot-rpo.md.
# Provider blocks cannot be for_each'd, so the policy is a small module
# instantiated once per region, mirroring how master-<inst>.tf wire the
# per-region provider alias. AWS Backup (backup.tf) stays the cluster-EBS
# vehicle and snapscheduler the in-cluster one; this file owns only the EC2
# masters.

# DLM execution role. IAM is global, so one role serves all five regions. The
# AWS-managed policy covers snapshot create/copy/delete on the default EBS keys;
# the inline grant adds the destination CMK so cross-region copies re-encrypt.
data "aws_iam_policy_document" "dlm_assume" {
  statement {
    effect  = "Allow"
    actions = ["sts:AssumeRole"]

    principals {
      type        = "Service"
      identifiers = ["dlm.amazonaws.com"]
    }
  }
}

resource "aws_iam_role" "dlm" {
  name               = "${local.cluster_name}-dlm-snapshot"
  assume_role_policy = data.aws_iam_policy_document.dlm_assume.json
  tags               = local.tags
}

resource "aws_iam_role_policy_attachment" "dlm" {
  role       = aws_iam_role.dlm.name
  policy_arn = "arn:aws:iam::aws:policy/service-role/AWSDataLifecycleManagerServiceRole"
}

data "aws_iam_policy_document" "dlm_copy_kms" {
  statement {
    sid    = "CrossRegionCopyReEncrypt"
    effect = "Allow"

    actions = [
      "kms:Encrypt",
      "kms:Decrypt",
      "kms:ReEncrypt*",
      "kms:GenerateDataKey*",
      "kms:DescribeKey",
      "kms:CreateGrant",
    ]

    resources = [aws_kms_key.backup.arn]
  }
}

resource "aws_iam_role_policy" "dlm_copy_kms" {
  name   = "cross-region-copy-kms"
  role   = aws_iam_role.dlm.id
  policy = data.aws_iam_policy_document.dlm_copy_kms.json
}

# One policy per master region. The eu-central-1 instance also covers pg by tag
# (see aws_ec2_tag below). var.aws_region (us-east-1) hosts no master and is the
# cross-region copy sink, reusing the existing backup CMK.
module "dlm_eu_central_1" {
  source    = "./modules/dlm-snapshot"
  providers = { aws = aws.eu-central-1 }

  region_label        = "eu-central-1"
  execution_role_arn  = aws_iam_role.dlm.arn
  copy_target_region  = var.aws_region
  copy_target_cmk_arn = aws_kms_key.backup.arn
  tags                = local.tags
}

module "dlm_us_west_2" {
  source    = "./modules/dlm-snapshot"
  providers = { aws = aws.us-west-2 }

  region_label        = "us-west-2"
  execution_role_arn  = aws_iam_role.dlm.arn
  copy_target_region  = var.aws_region
  copy_target_cmk_arn = aws_kms_key.backup.arn
  tags                = local.tags
}

module "dlm_us_west_1" {
  source    = "./modules/dlm-snapshot"
  providers = { aws = aws.us-west-1 }

  region_label        = "us-west-1"
  execution_role_arn  = aws_iam_role.dlm.arn
  copy_target_region  = var.aws_region
  copy_target_cmk_arn = aws_kms_key.backup.arn
  tags                = local.tags
}

module "dlm_us_east_2" {
  source    = "./modules/dlm-snapshot"
  providers = { aws = aws.us-east-2 }

  region_label        = "us-east-2"
  execution_role_arn  = aws_iam_role.dlm.arn
  copy_target_region  = var.aws_region
  copy_target_cmk_arn = aws_kms_key.backup.arn
  tags                = local.tags
}

module "dlm_eu_west_1" {
  source    = "./modules/dlm-snapshot"
  providers = { aws = aws.eu-west-1 }

  region_label        = "eu-west-1"
  execution_role_arn  = aws_iam_role.dlm.arn
  copy_target_region  = var.aws_region
  copy_target_cmk_arn = aws_kms_key.backup.arn
  tags                = local.tags
}

# pg's data volume is CFN-managed (master-pg.tf provisions only the worker
# fleet), so tag it in place to bring it under the eu-central-1 policy without
# importing the volume. status=in-use selects the live volume, not the detached
# leftover that shares the Name tag. Remove when pg migrates CFN to TF and
# module.pg owns the volume.
data "aws_ebs_volume" "pg_data" {
  provider    = aws.eu-central-1
  most_recent = true

  filter {
    name   = "tag:Name"
    values = ["jenkins-pg DATA, do not remove"]
  }

  filter {
    name   = "status"
    values = ["in-use"]
  }
}

resource "aws_ec2_tag" "pg_data_snapshot_policy" {
  provider    = aws.eu-central-1
  resource_id = data.aws_ebs_volume.pg_data.id
  key         = "snapshot-policy"
  value       = "jenkins-master"
}
