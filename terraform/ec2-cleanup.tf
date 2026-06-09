# Owner: platform
#
# Reaper of untagged EC2 instances (+ orphan eksctl-* stack cleanup) across
# all regions.
#
# One function in us-east-1 (default provider); the handler enumerates regions
# via describe_regions(), so the policy carries NO aws:RequestedRegion
# condition. ./modules/scheduled-lambda owns the wiring; this file owns the
# least-privilege policy. Tunables (schedule, dry_run, EKS skip regex, timeout)
# live in the "Cleanup Lambda parameters" section of locals.tf.

locals {
  ec2_cleanup_acct = data.aws_caller_identity.current.account_id
}

data "aws_iam_policy_document" "ec2_cleanup" {
  statement {
    sid       = "DescribeForScan"
    effect    = "Allow"
    actions   = ["ec2:DescribeRegions", "ec2:DescribeInstances", "ec2:DescribeSecurityGroups"]
    resources = ["*"] # Describe* has no resource-level scoping.
  }

  # NO tag condition: iit-billing-tag is a unix-epoch expiry when numeric, so
  # tagged-but-expired instances are also eligible and IAM cannot compare a tag
  # to the clock. Scope to account instance ARNs instead.
  statement {
    sid       = "TerminateInstances"
    effect    = "Allow"
    actions   = ["ec2:TerminateInstances"]
    resources = ["arn:aws:ec2:*:${local.ec2_cleanup_acct}:instance/*"]
  }

  statement {
    sid     = "DeleteOrphanEksctlStacks"
    effect  = "Allow"
    actions = ["cloudformation:DescribeStacks", "cloudformation:DescribeStackEvents", "cloudformation:DeleteStack"]
    # Only the eksctl-<cluster>-cluster stacks the reaper targets.
    resources = ["arn:aws:cloudformation:*:${local.ec2_cleanup_acct}:stack/eksctl-*-cluster/*"]
  }

  # The handler's DELETE_FAILED remediation calls these, so they must be
  # granted. They are the dangerous grants -- resource-level scoping for the
  # Revoke/Route actions is weak (sg-/rtb- physical IDs), so safety rests on the
  # handler only invoking them for resources of an eksctl-*-cluster stack it is
  # already deleting.
  statement {
    sid    = "EksDeleteFailedRemediation"
    effect = "Allow"
    actions = [
      "ec2:RevokeSecurityGroupIngress",
      "ec2:DisassociateRouteTable",
      "ec2:DeleteRoute",
    ]
    resources = ["*"]
  }
}

module "ec2_cleanup_lambda" {
  source = "./modules/scheduled-lambda"

  name        = "ec2-cleanup"
  name_prefix = "${local.cluster_name}-"
  description = "Reaper of untagged EC2 + orphan eksctl stack cleanup."

  source_dir          = "${path.module}/lambdas/ec2-cleanup"
  handler             = "index.lambda_handler"
  runtime             = local.cleanup_lambda_runtime
  schedule_expression = local.ec2_cleanup.schedule
  timeout             = local.ec2_cleanup.timeout

  environment_variables = {
    DRY_RUN                  = local.ec2_cleanup.dry_run
    EKS_SKIP_PATTERN         = local.ec2_cleanup.eks_skip_pattern
    MOLECULE_BILLING_PATTERN = local.ec2_cleanup.molecule_billing_pattern
    MOLECULE_MAX_AGE_HOURS   = local.ec2_cleanup.molecule_max_age_hours
  }

  permissions_policy_json = data.aws_iam_policy_document.ec2_cleanup.json
  tags                    = local.tags
}

output "ec2_cleanup_lambda_arn" {
  description = "ARN of the ec2-cleanup Lambda."
  value       = module.ec2_cleanup_lambda.function_arn
}
