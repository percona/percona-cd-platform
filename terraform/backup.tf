# AWS Backup plan for the cluster's stateful EBS volumes.
#
# StorageClass `reclaimPolicy: Retain` on gp3-monitoring-1a-retain and
# gp3-jenkins-1a-retain only stops Kubernetes from deleting the volume — it
# does not protect against AZ outage, EBS data corruption, accidental
# snapshot pruning, or operator error. AWS Backup gives us:
#   - daily snapshots, KMS-encrypted (separate CMK from the cluster CMK)
#   - 14-day retention (covers a full incident-response window)
#   - tag-based selection so new stateful workloads are auto-protected
#     by adding `workload=<name>` to their StorageClass tagSpecification
#
# Resources auto-detected: any EC2 EBS volume whose tags match the
# selection_tag block, in the same region as the vault. The EBS-CSI driver
# propagates `workload=prometheus` (resources/addons/storageclass-gp3/
# templates/storageclasses.yaml) and `workload=jenkins` onto the AWS volume,
# so we get coverage automatically when a PVC binds.

# Separate CMK for the backup vault — keeps backup-encryption blast radius
# distinct from the cluster CMK (eks.tf -> module.eks.kms_key_arn). If the
# cluster CMK ever has to be rotated/disabled in an incident, backups stay
# readable; conversely, restricting backup-role IAM doesn't bleed into
# cluster-secret encryption.
resource "aws_kms_key" "backup" {
  description             = "AWS Backup vault encryption — ${local.cluster_name}"
  deletion_window_in_days = 7
  enable_key_rotation     = true

  tags = local.tags
}

resource "aws_kms_alias" "backup" {
  name          = "alias/${local.cluster_name}-backup"
  target_key_id = aws_kms_key.backup.key_id
}

resource "aws_backup_vault" "main" {
  name        = "${local.cluster_name}-backups"
  kms_key_arn = aws_kms_key.backup.arn

  tags = local.tags
}

resource "aws_backup_plan" "daily" {
  name = "${local.cluster_name}-daily"

  rule {
    rule_name         = "daily-0300-utc"
    target_vault_name = aws_backup_vault.main.name
    schedule          = "cron(0 3 * * ? *)" # 03:00 UTC daily

    lifecycle {
      delete_after = 14
    }
  }

  tags = local.tags
}

# Service role assumed by AWS Backup itself. Two AWS-managed policies cover
# the full backup + restore lifecycle for EBS (and any future RDS / EFS / DynamoDB
# resources that pick up the same selection tag).
resource "aws_iam_role" "backup" {
  name = "${local.cluster_name}-aws-backup"

  assume_role_policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Effect    = "Allow"
      Principal = { Service = "backup.amazonaws.com" }
      Action    = "sts:AssumeRole"
    }]
  })

  tags = local.tags
}

resource "aws_iam_role_policy_attachment" "backup" {
  role       = aws_iam_role.backup.name
  policy_arn = "arn:aws:iam::aws:policy/service-role/AWSBackupServiceRolePolicyForBackup"
}

resource "aws_iam_role_policy_attachment" "backup_restore" {
  role       = aws_iam_role.backup.name
  policy_arn = "arn:aws:iam::aws:policy/service-role/AWSBackupServiceRolePolicyForRestores"
}

# Tag-based selections, one per workload. `selection_tag` is STRINGEQUALS-only
# and multiple `selection_tag` blocks within a single selection are AND'd —
# so to cover both monitoring + jenkins we need two resources. Adding a new
# stateful workload (e.g. mimir, registry) is one line: append its short
# name to local.backup_workloads and add `tagSpecification_4: workload=<name>`
# to its StorageClass.
locals {
  backup_workloads = toset(["prometheus", "jenkins"])
}

resource "aws_backup_selection" "by_workload" {
  for_each = local.backup_workloads

  name         = "${local.cluster_name}-${each.key}"
  iam_role_arn = aws_iam_role.backup.arn
  plan_id      = aws_backup_plan.daily.id

  selection_tag {
    type  = "STRINGEQUALS"
    key   = "workload"
    value = each.key
  }
}
