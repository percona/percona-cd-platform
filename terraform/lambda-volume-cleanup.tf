# Copyright (C) 2026 Percona LLC
#
# Daily reaper of unattached (available) EBS volumes across all regions.
#
# One function in us-east-1 (default provider); the handler iterates a fixed
# 8-region list via the SDK, so the policy carries NO aws:RequestedRegion
# condition. ./modules/scheduled-lambda owns the wiring; this file owns the
# least-privilege policy. Tunables (schedule, dry_run, age floor) live in the
# "Cleanup Lambda parameters" section of locals.tf.

data "aws_iam_policy_document" "volume_cleanup" {
  statement {
    sid       = "DescribeVolumesAllRegions"
    effect    = "Allow"
    actions   = ["ec2:DescribeVolumes"]
    resources = ["*"] # Describe* has no resource-level scoping.
  }

  statement {
    sid       = "DeleteUnattachedVolumes"
    effect    = "Allow"
    actions   = ["ec2:DeleteVolume"]
    resources = ["arn:aws:ec2:*:${data.aws_caller_identity.current.account_id}:volume/*"]
  }
}

module "volume_cleanup_lambda" {
  source = "./modules/scheduled-lambda"

  name        = "volume-cleanup"
  name_prefix = "${local.cluster_name}-"
  description = "Daily reaper of unattached EBS volumes across all regions."

  source_dir          = "${path.module}/lambdas/volume-cleanup"
  handler             = "index.lambda_handler"
  runtime             = local.cleanup_lambda_runtime
  schedule_expression = local.volume_cleanup.schedule

  environment_variables = {
    DRY_RUN       = local.volume_cleanup.dry_run
    MIN_AGE_HOURS = local.volume_cleanup.min_age_hours
  }

  permissions_policy_json = data.aws_iam_policy_document.volume_cleanup.json
  tags                    = local.tags
}

output "volume_cleanup_lambda_arn" {
  description = "ARN of the volume-cleanup Lambda."
  value       = module.volume_cleanup_lambda.function_arn
}
