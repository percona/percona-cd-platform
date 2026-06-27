# Owner: platform
# IAM role assumed by the build-mtr-ingest-image GitHub Actions workflow in
# Percona/percona-cd-platform via OIDC, used ONLY by the push-to-main job to
# push the mtr-ingest image (built from the vendored source in images/mtr-ingest/)
# to the percona-cd/mtr-ingest ECR repo (terraform/ecr.tf). PR builds DO NOT
# assume this role (the PR job carries no id-token), so PR code can never publish.
#
# Trust shape (federated OIDC, StringEquals aud+sub, shared per-account OIDC
# provider) is owned by ./modules/github-oidc-role. This file owns only the
# subject allowlist + the least-privilege permissions policy.
#
# Subject is the LOWERCASE canonical repo casing (StringEquals is case-
# sensitive) and ONLY refs/heads/main. Deliberately NO `:pull_request` claim,
# which would let forked-PR code assume a publish role.
#
# Blast radius if an OIDC token leaks before it expires:
#   - Push image layers + PutImage to ONLY percona-cd/mtr-ingest (immutable
#     tags reject overwrite of an existing <version>-<gitsha>).
#   - GetAuthorizationToken is account-wide (cannot be resource-scoped) but only
#     mints a docker login; the push actions stay bound to the one repo ARN.
#   - CANNOT delete images, mutate the lifecycle policy, create/delete repos,
#     read or write S3 / Secrets Manager / KMS, reach EKS, or touch any other
#     ECR repository.

# Least-privilege ECR push policy. GetAuthorizationToken MUST be Resource: *
# (ECR does not support resource-level perms on that action); every other action
# is scoped to the single repo ARN. No ecr:*, no Batch*Delete*, no
# PutLifecyclePolicy, no repo create/delete.
data "aws_iam_policy_document" "gha_mtr_ingest_image_push_perms" {
  statement {
    sid       = "EcrAuthToken"
    effect    = "Allow"
    actions   = ["ecr:GetAuthorizationToken"]
    resources = ["*"]
  }

  statement {
    sid    = "EcrPushToMtrIngestRepoOnly"
    effect = "Allow"
    actions = [
      "ecr:BatchCheckLayerAvailability",
      "ecr:InitiateLayerUpload",
      "ecr:UploadLayerPart",
      "ecr:CompleteLayerUpload",
      "ecr:PutImage",
      # Read-only: buildx issues a HEAD on the manifest when pushing a multi-arch
      # manifest list with provenance + SBOM attestations, so the push 403s
      # without it. Repo-scoped.
      "ecr:BatchGetImage",
    ]
    resources = [aws_ecr_repository.mtr_ingest.arn]
  }
}

# Trust + role wiring delegated to the in-repo module (shared OIDC provider data
# source). Single subject: lowercase repo, refs/heads/main only.
module "gha_mtr_ingest_image_push" {
  source = "./modules/github-oidc-role"

  name             = "gha-mtr-ingest-image-push"
  role_name_prefix = "${local.cluster_name}-"
  description      = "Assumed by the build-mtr-ingest-image GHA workflow (push-to-main job only) in percona/percona-cd-platform via OIDC to push the mtr-ingest image to percona-cd/mtr-ingest ECR. No PR/tag claims; least-privilege ECR push."

  subject_claims = [
    "repo:percona/percona-cd-platform:ref:refs/heads/main",
  ]

  permissions_policy_json = data.aws_iam_policy_document.gha_mtr_ingest_image_push_perms.json

  tags = local.tags
}

# Surface the role ARN for the workflow's aws-actions/configure-aws-credentials
# `role-to-assume`. ARN is not a secret.
output "gha_mtr_ingest_image_push_role_arn" {
  description = "Role ARN to set as `role-to-assume` in the build-mtr-ingest-image GHA workflow push-to-main job."
  value       = module.gha_mtr_ingest_image_push.role_arn
}
