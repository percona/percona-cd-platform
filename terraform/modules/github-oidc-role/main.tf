# Owner: platform
# Reusable GitHub Actions OIDC role module.
#
# Why this module exists:
#   Every GHA -> AWS role in this account follows the same shape (federated
#   OIDC, StringEquals aud + sub, one workload-specific permissions policy).
#   Hand-rolling that shape per workload makes it easy to ship an unsafe
#   trust policy (wildcard sub, missing aud check). This module owns the
#   shape so the caller only needs to think about the workload's permissions.
#
# Design choices that are load-bearing:
#
#   - StringEquals on `sub` (NOT StringLike). The caller passes an explicit
#     enumeration of valid `repo:<owner>/<repo>:<ref>` subjects. Wildcard
#     subs (`repo:owner/repo:*`) are a well-known GHA OIDC footgun: a forked
#     PR push, a tag from a malicious actor, or an environment trick can
#     mint a token whose `sub` matches the wildcard but should NOT be
#     allowed to assume the role. The module's `subject_claims` validators
#     reject empty lists and any sub containing `*` or `?`. If you
#     genuinely want StringLike, fork this module rather than weakening
#     it -- forcing the fork makes the decision visible in code review.
#
#   - StringEquals on `aud`. AWS STS requires `sts.amazonaws.com`; the
#     `audience` variable exists for non-AWS audience reuse (Cognito,
#     custom OIDC consumer).
#
#   - The OIDC provider is looked up via data source, not created. The
#     provider is a per-account singleton (one `token.actions.githubusercontent.com`
#     per AWS account). Multiple roles -- this module included -- share it.
#
#   - The caller owns `permissions_policy_json`. The module deliberately
#     does NOT try to compose region/tag conditions on the caller's behalf.
#     Workload-specific policies (EC2 RunInstances vs Bedrock InvokeModel
#     vs S3 reads) have wildly different shapes and conditions; the right
#     place for that knowledge is the workload's instantiation file.
#
# GitHub OIDC subject claim format (2026 update):
#
#   On 2026-04-23 GitHub announced that repositories created after
#   2026-06-18 issue ID-suffixed subjects: the `sub` claim embeds
#   `repository_id` and `repository_owner_id` so the claim is stable
#   across owner/repo renames. Legacy `repo:<owner>/<name>:...` subjects
#   stay for repositories that predate the cutoff. Roles for legacy repos
#   keep working unchanged. Roles for NEW repositories (post-2026-06-18)
#   should be configured to match on `repository_id` /
#   `repository_owner_id` instead of repo name, and the workflow needs
#   to opt into the new claim format via `id-token.permissions` + the
#   2026-04-23 GHA workflow-syntax addition.

data "aws_iam_openid_connect_provider" "github_actions" {
  url = "https://token.actions.githubusercontent.com"
}

data "aws_iam_policy_document" "trust" {
  statement {
    sid     = "GithubActionsOidcAssume"
    effect  = "Allow"
    actions = ["sts:AssumeRoleWithWebIdentity"]

    principals {
      type        = "Federated"
      identifiers = [data.aws_iam_openid_connect_provider.github_actions.arn]
    }

    condition {
      test     = "StringEquals"
      variable = "token.actions.githubusercontent.com:aud"
      values   = [var.audience]
    }

    # Explicit enumeration; never StringLike. See the header comment for
    # the rationale.
    condition {
      test     = "StringEquals"
      variable = "token.actions.githubusercontent.com:sub"
      values   = var.subject_claims
    }
  }
}

locals {
  # IAM role + policy names are formed as `<prefix><name>`. The prefix is
  # caller-supplied (typically `"${local.cluster_name}-"`) so the cluster
  # scope is visible in IAM listings; leave it empty to use `var.name`
  # verbatim.
  resource_name = "${var.role_name_prefix}${var.name}"
}

resource "aws_iam_policy" "this" {
  name        = local.resource_name
  description = var.description
  policy      = var.permissions_policy_json
  tags        = var.tags
}

resource "aws_iam_role" "this" {
  name               = local.resource_name
  description        = var.description
  assume_role_policy = data.aws_iam_policy_document.trust.json
  tags               = var.tags
}

resource "aws_iam_role_policy_attachment" "this" {
  role       = aws_iam_role.this.name
  policy_arn = aws_iam_policy.this.arn
}
