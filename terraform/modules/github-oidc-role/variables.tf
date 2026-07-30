# Required inputs are at the top. The module is deliberately thin: it does
# NOT try to be smart about the per-workload permissions policy. The caller
# renders an `aws_iam_policy_document` (or hand-writes JSON) and passes it
# in via `permissions_policy_json`; the module owns only the trust shape
# (federated OIDC, StringEquals aud + sub) and the wiring (policy + role +
# attachment + outputs).

# ----- Required -----

variable "name" {
  description = "Short workload name (e.g. \"gha-percona-server-ec2-fallback\"). Used as the suffix in the IAM role + policy names (caller typically passes the cluster_name via role_name_prefix). Keep <=40 chars to leave room for the prefix under the 64-char IAM name cap."
  type        = string
}

variable "description" {
  description = "Free-form description, surfaced on both the IAM role and the IAM policy. Reference the originating Jira key so an operator landing on the role in the AWS console can find the context."
  type        = string
}

variable "subject_claims" {
  description = "Explicit allowlist of GitHub Actions `sub` claims this role will accept (e.g. [\"repo:owner/repo:ref:refs/heads/main\", \"repo:owner/repo:pull_request\"]). Enforced via StringEquals -- NOT StringLike. Wildcard subs are a known privilege-escalation footgun (forked PR pushes, tag/environment tricks), so this module forbids them at the API."
  type        = list(string)

  validation {
    condition     = length(var.subject_claims) > 0
    error_message = "subject_claims must list at least one explicit sub; wildcard / empty allowlists are not supported."
  }

  validation {
    condition     = alltrue([for sub in var.subject_claims : !can(regex("[*?]", sub))])
    error_message = "subject_claims must be exact sub strings; wildcard characters (* or ?) are not supported. The trust uses StringEquals, where they would be dead weight at best."
  }
}

variable "permissions_policy_json" {
  description = "Rendered JSON document with the role's per-workload permissions. The caller is responsible for least-privilege: condition keys, resource ARNs, region scoping. The module wraps this verbatim into an `aws_iam_policy` and attaches it to the role."
  type        = string
}

variable "tags" {
  description = "Tags merged into the IAM role + policy. Pass `local.tags` from the root module so the percona-dev-admin cleanup tags (`iit-billing-tag`, `PerconaKeep`) flow through."
  type        = map(string)
}

# ----- Optional -----

variable "audience" {
  description = "OIDC `aud` claim the role accepts. AWS STS requires `sts.amazonaws.com` for the standard `configure-aws-credentials` GHA action; the variable exists for forward-compat with non-AWS audiences (e.g. when the role is reused for Cognito or a custom OIDC consumer)."
  type        = string
  default     = "sts.amazonaws.com"
}

variable "role_name_prefix" {
  description = "Optional prefix prepended to var.name when forming the IAM role + policy names. Defaults to empty; the caller normally embeds their cluster_name in the prefix (e.g. local.cluster_name plus a dash) to keep the cluster scope visible in IAM. Leave empty if you want the role named exactly var.name."
  type        = string
  default     = ""
}
