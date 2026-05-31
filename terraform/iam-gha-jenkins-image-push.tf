# IAM role assumed by the build-jenkins-image GitHub Actions workflow in
# Percona/percona-cd-platform via OIDC, used ONLY by the push-to-main job to
# push the baked Jenkins controller image to the percona-cd/jenkins-percona
# ECR repo (terraform/ecr.tf). PR builds DO NOT assume this role (the PR job
# carries no id-token), so PR code can never publish an image.
#
# Tickets: TIER-5 substrate (Jenkins-on-k8s controller-image pipeline).
#
# Trust shape (federated OIDC, StringEquals aud+sub, shared per-account OIDC
# provider) is owned by ./modules/github-oidc-role. This file owns only the
# subject allowlist + the least-privilege permissions policy.
#
# Subject is the LOWERCASE canonical repo casing (StringEquals is
# case-sensitive) and ONLY refs/heads/main. Deliberately NOT including a
# `:pull_request` claim — copying that from iam-gha-percona-server-ec2-
# fallback.tf:316 would let forked-PR code assume a publish role. If a tag/
# release ever becomes the publish trigger, add a SEPARATE, explicitly
# reviewed role; do not widen this one.
#
# Blast radius if an OIDC token leaks before it expires (15 min default):
#   - Attacker can push image layers + PutImage to ONLY
#     percona-cd/jenkins-percona (immutable tags reject overwrite of an
#     existing <lts>-<gitsha>).
#   - GetAuthorizationToken is account-wide (it cannot be resource-scoped),
#     but that only mints a docker login; the push actions are still bound to
#     the one repo ARN, so no other ECR repo can be written.
#   - Attacker CANNOT: delete images, mutate the lifecycle policy, create/
#     delete repos, read or write S3 / Secrets Manager / KMS, reach EKS, or
#     touch any other ECR repository.

# Least-privilege ECR push policy. GetAuthorizationToken MUST be Resource: *
# (ECR does not support resource-level perms on that action); every other
# action is scoped to the single repo ARN. No ecr:*, no Batch*Delete*, no
# PutLifecyclePolicy, no repo create/delete.
data "aws_iam_policy_document" "gha_jenkins_image_push_perms" {
  statement {
    sid       = "EcrAuthToken"
    effect    = "Allow"
    actions   = ["ecr:GetAuthorizationToken"]
    resources = ["*"]
  }

  statement {
    sid    = "EcrPushToJenkinsRepoOnly"
    effect = "Allow"
    actions = [
      "ecr:BatchCheckLayerAvailability",
      "ecr:InitiateLayerUpload",
      "ecr:UploadLayerPart",
      "ecr:CompleteLayerUpload",
      "ecr:PutImage",
    ]
    resources = [aws_ecr_repository.jenkins_percona.arn]
  }
}

# Trust + role wiring delegated to the in-repo module (shared OIDC provider
# data source). Single subject: lowercase repo, refs/heads/main only.
module "gha_jenkins_image_push" {
  source = "./modules/github-oidc-role"

  name             = "gha-jenkins-image-push"
  role_name_prefix = "${local.cluster_name}-"
  description      = "Assumed by the build-jenkins-image GHA workflow (push-to-main job only) in percona/percona-cd-platform via OIDC to push the baked Jenkins controller image to percona-cd/jenkins-percona ECR. No PR/tag claims; least-privilege ECR push."

  subject_claims = [
    "repo:percona/percona-cd-platform:ref:refs/heads/main",
  ]

  permissions_policy_json = data.aws_iam_policy_document.gha_jenkins_image_push_perms.json

  tags = local.tags
}

# Surface the role ARN for the workflow's aws-actions/configure-aws-credentials
# `role-to-assume`. ARN is not a secret.
output "gha_jenkins_image_push_role_arn" {
  description = "Role ARN to set as `role-to-assume` in the build-jenkins-image GHA workflow push-to-main job (TIER-5 controller image push)."
  value       = module.gha_jenkins_image_push.role_arn
}
