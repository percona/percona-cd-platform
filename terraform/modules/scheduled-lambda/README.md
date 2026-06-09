# scheduled-lambda

A cron-scheduled AWS Lambda, wired end to end: zipped source, EventBridge rule
+ target + invoke permission, log group with retention, and an execution role
that carries one caller-supplied workload permissions policy.

Built for the account's cleanup reapers (EBS volume cleanup, untagged-EC2
cleanup), but it is workload-agnostic.

## Design decisions

- **Caller owns the permissions.** The module attaches the AWS-managed
  `AWSLambdaBasicExecutionRole` (CloudWatch Logs only) and nothing else; the
  destructive grants come in via `permissions_policy_json`, rendered by the
  caller as an `aws_iam_policy_document`. Do **not** put `logs:*` in that JSON.
- **Legacy EventBridge Rule, not Scheduler.** For a plain `rate()`/`cron()`
  trigger, Scheduler adds a second IAM role/trust surface for no benefit. The
  Rule + `aws_lambda_permission` (with `source_arn`) is the smaller surface.
- **`source_arn` is always set** on the invoke permission (Trivy AWS-0067).
- **`reserved_concurrent_executions = 1`** by default: a destructive reaper
  must never overlap itself.
- **One function, many regions in code.** The reapers iterate AWS regions via
  the SDK, so this deploys a single function in the default-provider region
  (us-east-1). The execution policy must therefore NOT carry an
  `aws:RequestedRegion` condition.
- **Tags flow from the root `local.tags`**, including `PerconaKeep=True`, so the
  reaper never deletes its own backing resources.

## Usage

```hcl
data "aws_iam_policy_document" "volume_cleanup" {
  statement {
    sid       = "DescribeVolumes"
    effect    = "Allow"
    actions   = ["ec2:DescribeVolumes"]
    resources = ["*"] # Describe* has no resource-level scoping
  }
  statement {
    sid       = "DeleteUnattachedVolumes"
    effect    = "Allow"
    actions   = ["ec2:DeleteVolume"]
    resources = ["arn:aws:ec2:*:${data.aws_caller_identity.current.account_id}:volume/*"]
  }
}

module "volume_cleanup" {
  source = "./modules/scheduled-lambda"

  name        = "volume-cleanup"
  name_prefix = "${local.cluster_name}-"
  description = "Daily reaper of unattached EBS volumes across all regions. PS-11262."

  source_dir          = "${path.module}/lambdas/volume-cleanup"
  handler             = "index.lambda_handler"
  runtime             = "python3.14"
  schedule_expression = "rate(1 day)"

  environment_variables = {
    DRY_RUN       = "true" # flip to "false" after the dry-run bake-in
    MIN_AGE_HOURS = "24"
  }

  permissions_policy_json = data.aws_iam_policy_document.volume_cleanup.json
  tags                    = local.tags
}
```

## Inputs

Required: `name`, `description`, `source_dir`, `handler`, `runtime`,
`schedule_expression`, `permissions_policy_json`, `tags`.

Optional (defaults): `name_prefix` (`""`), `environment_variables` (`{}`),
`timeout` (`300`), `memory_size` (`128`), `architectures` (`["arm64"]`),
`reserved_concurrent_executions` (`1`), `schedule_enabled` (`true`),
`log_retention_days` (`14`), `tracing_mode` (`null`).

## Outputs

`function_arn`, `function_name`, `role_arn`, `role_name`, `log_group_name`,
`event_rule_arn`.
