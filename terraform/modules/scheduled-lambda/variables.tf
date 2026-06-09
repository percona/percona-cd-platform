# Required inputs are at the top. Like the github-oidc-role module, this one
# is deliberately thin: it owns the WIRING of a cron-scheduled Lambda (function
# package, EventBridge rule + target + invoke permission, log group, execution
# role) but NOT the workload's permissions. The caller renders an
# `aws_iam_policy_document` and passes it via `permissions_policy_json`; the
# module attaches it to the function role alongside the AWS-managed basic
# execution policy (CloudWatch Logs only).

# ----- Required -----

variable "name" {
  description = "Short workload name (e.g. \"volume-cleanup\"). Used as the suffix for the function, role, policy, rule and log-group names (caller normally passes cluster_name via name_prefix). Keep <=40 chars to stay under the 64-char IAM/function name caps after the prefix."
  type        = string

  validation {
    condition     = length(var.name) > 0 && length(var.name) <= 40
    error_message = "name must be 1-40 characters."
  }
}

variable "description" {
  description = "Free-form description surfaced on the function, role and policy. Reference the originating ticket (e.g. PS-11262) so an operator landing on the resource in the console can find the context."
  type        = string
}

variable "source_dir" {
  description = "Absolute or path.module-relative directory containing the Lambda source (e.g. an index.py). Zipped verbatim by data.archive_file; source_code_hash tracks its contents so code changes are published on apply."
  type        = string
}

variable "handler" {
  description = "Lambda handler entrypoint, e.g. \"index.lambda_handler\"."
  type        = string
}

variable "runtime" {
  description = "Lambda managed runtime identifier, e.g. \"python3.14\" (the latest supported Python runtime). See https://docs.aws.amazon.com/lambda/latest/dg/lambda-runtimes.html."
  type        = string

  validation {
    condition     = length(var.runtime) > 0
    error_message = "runtime must be set (e.g. python3.14)."
  }
}

variable "schedule_expression" {
  description = "EventBridge schedule expression for the trigger rule, e.g. \"rate(1 day)\" or \"cron(0 3 * * ? *)\"."
  type        = string

  validation {
    condition     = length(var.schedule_expression) > 0
    error_message = "schedule_expression must be set (rate(...) or cron(...))."
  }
}

variable "permissions_policy_json" {
  description = "Rendered JSON document with the function's per-workload permissions (the destructive cleanup grants). The caller owns least-privilege: action list, resource ARNs, condition keys. The module wraps it into an aws_iam_policy and attaches it to the function role. CloudWatch Logs perms come separately from the AWS-managed basic-execution policy, so do NOT include logs:* here."
  type        = string
}

variable "tags" {
  description = "Tags merged onto every resource. Pass `local.tags` from the root so the percona-dev-admin cleanup tags (iit-billing-tag, PerconaKeep=True) flow through -- PerconaKeep keeps the reaper from deleting its own backing resources."
  type        = map(string)
}

# ----- Optional -----

variable "name_prefix" {
  description = "Prefix prepended to var.name for all resource names. Caller normally passes the cluster_name plus a dash (local.cluster_name) so the cluster scope is visible. Leave empty to name resources exactly var.name."
  type        = string
  default     = ""
}

variable "environment_variables" {
  description = "Environment variables passed to the function (e.g. DRY_RUN, MIN_AGE_HOURS, REGIONS, EKS_SKIP_PATTERN). The handlers read these at invocation time, so flipping DRY_RUN is a plain apply."
  type        = map(string)
  default     = {}
}

variable "timeout" {
  description = "Function timeout in seconds. A multi-region scan needs headroom; 300s is the default."
  type        = number
  default     = 300
}

variable "memory_size" {
  description = "Function memory in MB (also scales CPU)."
  type        = number
  default     = 128
}

variable "architectures" {
  description = "Instruction set for the function. arm64 (Graviton) is cheaper and is what the other cleanup Lambdas use."
  type        = list(string)
  default     = ["arm64"]
}

variable "reserved_concurrent_executions" {
  description = "Reserved concurrency. Defaults to 1 so a slow run can never overlap itself (a real safety control for a destructive reaper, not just scanner appeasement); a second concurrent trigger is throttled rather than racing the first."
  type        = number
  default     = 1
}

variable "schedule_enabled" {
  description = "Whether the EventBridge rule is ENABLED. Set false to deploy the function dormant (e.g. for a DRY_RUN bake-in) and enable it in a later apply."
  type        = bool
  default     = true
}

variable "log_retention_days" {
  description = "CloudWatch Logs retention for the function log group."
  type        = number
  default     = 14
}

variable "tracing_mode" {
  description = "X-Ray tracing mode (\"Active\" or \"PassThrough\"); null disables tracing. Optional -- X-Ray is below the repo's HIGH/CRITICAL Trivy gate."
  type        = string
  default     = null

  validation {
    condition     = var.tracing_mode == null || contains(["Active", "PassThrough"], var.tracing_mode)
    error_message = "tracing_mode must be null, \"Active\", or \"PassThrough\"."
  }
}
