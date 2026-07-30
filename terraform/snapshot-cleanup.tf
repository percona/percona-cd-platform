# Owner: platform
#
# Daily reaper of aged-out ebs-csi (snapscheduler) EBS snapshots in the cluster
# region. Closes the loop the in-cluster retention cannot: snapscheduler prunes
# only the VolumeSnapshot objects, and the ebs-csi-retain class keeps the
# physical snapshot on purpose (deletionPolicy Retain, so a GitOps prune cannot
# erase recovery points), which left the physical snapshots growing without
# bound. Selection is the managed-by=ebs-csi-driver tag the class stamps at
# create time (resources/addons/storageclass-gp3). The DeleteSnapshot grant
# repeats that tag as an IAM condition, so even a handler bug cannot delete a
# snapshot outside the CSI class. DLM (ebs-snapshots.tf), AWS Backup
# (backup.tf) and the manual CFN-orphan runbook keep their own vehicles.
# One function in us-east-1 (the CSI driver only snapshots cluster volumes
# there). Tunables (schedule, dry_run, retention) live in the "Cleanup Lambda
# parameters" section of locals.tf.

data "aws_iam_policy_document" "snapshot_cleanup" {
  statement {
    sid       = "DescribeSnapshotsAllRegions"
    effect    = "Allow"
    actions   = ["ec2:DescribeSnapshots"]
    resources = ["*"] # Describe* has no resource-level scoping.
  }

  statement {
    sid       = "DeleteCsiManagedSnapshots"
    effect    = "Allow"
    actions   = ["ec2:DeleteSnapshot"]
    resources = ["arn:aws:ec2:*::snapshot/*"] # snapshot ARNs carry no account id

    condition {
      test     = "StringEquals"
      variable = "aws:ResourceTag/managed-by"
      values   = ["ebs-csi-driver"]
    }
  }
}

module "snapshot_cleanup_lambda" {
  source = "./modules/scheduled-lambda"

  name        = "snapshot-cleanup"
  name_prefix = "${local.cluster_name}-"
  description = "Daily reaper of aged-out ebs-csi (snapscheduler) EBS snapshots."

  source_dir          = "${path.module}/lambdas/snapshot-cleanup"
  handler             = "index.lambda_handler"
  runtime             = local.cleanup_lambda_runtime
  schedule_expression = local.snapshot_cleanup.schedule

  environment_variables = {
    DRY_RUN        = local.snapshot_cleanup.dry_run
    RETENTION_DAYS = local.snapshot_cleanup.retention_days
    KEEP_COUNT     = local.snapshot_cleanup.keep_count
  }

  permissions_policy_json = data.aws_iam_policy_document.snapshot_cleanup.json
  tags                    = local.tags
}

output "snapshot_cleanup_lambda_arn" {
  description = "ARN of the snapshot-cleanup Lambda."
  value       = module.snapshot_cleanup_lambda.function_arn
}
