# Owner: platform
# Reusable cron-scheduled Lambda module.
#
# Why this module exists:
#   Every "scheduled cleanup reaper" in this account is the same shape: a
#   zipped Python function, an EventBridge rule that fires it on a cron, an
#   invoke permission scoped to that rule, a log group with retention, and an
#   execution role carrying one workload-specific permissions policy. Hand-
#   rolling that per reaper makes it easy to ship a Lambda permission with no
#   source_arn (Trivy AWS-0067, CRITICAL) or a function that can overlap
#   itself. This module owns the shape so the caller only thinks about the
#   workload's destructive permissions.
#
# Design choices that are load-bearing:
#
#   - Legacy EventBridge Rule (aws_cloudwatch_event_rule), NOT EventBridge
#     Scheduler. For a plain rate()/cron() trigger, Scheduler buys nothing and
#     adds a second IAM role + trust surface (the scheduler's invoke role).
#     The Rule + aws_lambda_permission(source_arn) is the smaller surface and
#     matches the rest of the repo.
#
#   - aws_lambda_permission ALWAYS carries source_arn = the rule ARN. This is
#     the Trivy AWS-0067 (CRITICAL) control: without it, any principal in the
#     events service could invoke the function.
#
#   - The caller owns permissions_policy_json. The module does NOT compose the
#     reaper's ec2:DeleteVolume / ec2:TerminateInstances grants; those have
#     workload-specific resource/condition shapes and belong in the
#     instantiation file. The module only adds the AWS-managed basic-execution
#     policy (CloudWatch Logs), so the caller's policy is pure workload.
#
#   - reserved_concurrent_executions defaults to 1: a destructive reaper must
#     never run concurrently with itself.
#
#   - The log group is created explicitly (with retention) and the function
#     depends on it, so retention is in place before the first invocation
#     instead of Lambda auto-creating a never-expiring group.

locals {
  # Function / role / policy / rule / log-group all share one name so they are
  # easy to correlate in the console. `<prefix><name>`, prefix usually the
  # cluster_name plus a dash.
  resource_name = "${var.name_prefix}${var.name}"
}

# ----- execution role (trust = Lambda only) -----

data "aws_iam_policy_document" "trust" {
  statement {
    sid     = "LambdaServiceAssume"
    effect  = "Allow"
    actions = ["sts:AssumeRole"]
    principals {
      type        = "Service"
      identifiers = ["lambda.amazonaws.com"]
    }
  }
}

resource "aws_iam_role" "this" {
  name               = local.resource_name
  description        = var.description
  assume_role_policy = data.aws_iam_policy_document.trust.json
  tags               = var.tags
}

# Workload (destructive) permissions: caller-owned, attached verbatim.
resource "aws_iam_policy" "this" {
  name        = local.resource_name
  description = var.description
  policy      = var.permissions_policy_json
  tags        = var.tags
}

resource "aws_iam_role_policy_attachment" "this" {
  role       = aws_iam_role.this.name
  policy_arn = aws_iam_policy.this.arn
}

# CloudWatch Logs perms come from the AWS-managed policy, so permissions_policy_json
# stays pure workload (no logs:* boilerplate).
resource "aws_iam_role_policy_attachment" "basic_execution" {
  role       = aws_iam_role.this.name
  policy_arn = "arn:aws:iam::aws:policy/service-role/AWSLambdaBasicExecutionRole"
}

# ----- package + function -----

data "archive_file" "this" {
  type        = "zip"
  source_dir  = var.source_dir
  output_path = "${path.module}/.build/${var.name}.zip"
  # Never bundle bytecode: it embeds source mtimes + an interpreter version, so
  # it makes the package hash non-deterministic and ships .pyc built for the
  # wrong runtime. Tests also set sys.dont_write_bytecode and the dir is
  # gitignored, so this is defense-in-depth.
  excludes = ["__pycache__", "*.pyc"]
}

resource "aws_cloudwatch_log_group" "this" {
  name              = "/aws/lambda/${local.resource_name}"
  retention_in_days = var.log_retention_days
  tags              = var.tags
}

resource "aws_lambda_function" "this" {
  function_name                  = local.resource_name
  description                    = var.description
  role                           = aws_iam_role.this.arn
  handler                        = var.handler
  runtime                        = var.runtime
  architectures                  = var.architectures
  memory_size                    = var.memory_size
  timeout                        = var.timeout
  reserved_concurrent_executions = var.reserved_concurrent_executions

  filename         = data.archive_file.this.output_path
  source_code_hash = data.archive_file.this.output_base64sha256

  dynamic "environment" {
    for_each = length(var.environment_variables) > 0 ? [1] : []
    content {
      variables = var.environment_variables
    }
  }

  dynamic "tracing_config" {
    for_each = var.tracing_mode == null ? [] : [1]
    content {
      mode = var.tracing_mode
    }
  }

  tags = var.tags

  # Retention-bearing log group must exist before the first invocation, and the
  # role must carry its policies before the function is wired live.
  depends_on = [
    aws_cloudwatch_log_group.this,
    aws_iam_role_policy_attachment.this,
    aws_iam_role_policy_attachment.basic_execution,
  ]
}

# ----- cron trigger -----

resource "aws_cloudwatch_event_rule" "this" {
  name                = local.resource_name
  description         = var.description
  schedule_expression = var.schedule_expression
  state               = var.schedule_enabled ? "ENABLED" : "DISABLED"
  tags                = var.tags
}

resource "aws_cloudwatch_event_target" "this" {
  rule = aws_cloudwatch_event_rule.this.name
  arn  = aws_lambda_function.this.arn
  # Create the invoke permission before wiring the target, so a schedule that
  # fires in the creation window is not denied.
  depends_on = [aws_lambda_permission.this]
}

resource "aws_lambda_permission" "this" {
  statement_id  = "AllowExecutionFromEventBridge"
  action        = "lambda:InvokeFunction"
  function_name = aws_lambda_function.this.function_name
  principal     = "events.amazonaws.com"
  source_arn    = aws_cloudwatch_event_rule.this.arn
}
