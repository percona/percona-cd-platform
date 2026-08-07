# Owner: pmm
# IAM role assumed by GitHub Actions workflows in `percona/pmm-qa` via OIDC
# federation, used to connect kubectl/helm test runs to the PMM HA QA EKS
# clusters (`pmm-ha*`) in us-east-2.
#
# Scope: this file owns the AWS-side half only. The role's single permission
# is `eks:DescribeCluster`, which is what `aws eks update-kubeconfig` calls
# to render a kubeconfig (cluster endpoint + CA). Kubernetes-side
# authorization is deliberately NOT granted here: the clusters are
# short-lived eksctl clusters created by the pmm3-ha-eks Jenkins job
# (Percona-Lab/jenkins-pipelines, pmm/v3/pmm3-ha-eks.groovy), and that job's
# access-entry stage is where this role's principal gets its EKS access
# entry + access policy at cluster create time. Splitting it that way keeps
# cluster lifecycle and cluster authorization with one owner (the creating
# job) and durable account-level IAM here.
#
# Public-repo safety (`percona/pmm-qa` is public):
#   - Trust is StringEquals on an explicit subject list (module-enforced;
#     no StringLike, no wildcards).
#   - The `pull_request` subject only matches workflow runs in the base
#     repository context. Fork-triggered `pull_request` runs cannot request
#     `id-token: write`, so fork PR code cannot mint a token that assumes
#     this role.
#
# Trust shape is owned by the `github-oidc-role` module
# (`terraform/modules/github-oidc-role/`). This file owns only the subject
# allowlist + the workload permissions policy.
#
# Blast radius if a workflow's OIDC token leaks before it expires (15 min
# default):
#   - Attacker can call eks:DescribeCluster on clusters named `pmm-ha*` in
#     us-east-2 (returns endpoint URL + cluster CA, not credentials).
#   - Attacker can reach the Kubernetes API of any live `pmm-ha*` cluster
#     whose access entries include this role (ephemeral QA clusters,
#     reaped by the paired cleanup job on a retention timer).
#   - Attacker CANNOT: touch any other EKS cluster (percona-ci-platform
#     included, since it carries no access entry for this role), EC2, IAM,
#     S3, Secrets Manager, or any AWS write action.

data "aws_iam_policy_document" "gha_pmm_qa_eks_perms" {
  # update-kubeconfig's only required call. Name-scoped so the role cannot
  # describe unrelated clusters in the account.
  statement {
    sid       = "DescribePmmHaClustersOnly"
    effect    = "Allow"
    actions   = ["eks:DescribeCluster"]
    resources = ["arn:aws:eks:us-east-2:${data.aws_caller_identity.current.account_id}:cluster/pmm-ha*"]
  }
}

module "gha_pmm_qa_eks" {
  source = "./modules/github-oidc-role"

  name             = "gha-pmm-qa-eks"
  role_name_prefix = "${local.cluster_name}-"
  description      = "Assumed by GitHub Actions workflows in percona/pmm-qa via OIDC to build a kubeconfig for the pmm-ha* QA EKS clusters in us-east-2 (PKG-1435). Kubernetes-side authorization comes from EKS access entries granted by the cluster-creating Jenkins job."

  subject_claims = [
    "repo:percona/pmm-qa:ref:refs/heads/main",
    "repo:percona/pmm-qa:pull_request",
  ]

  permissions_policy_json = data.aws_iam_policy_document.gha_pmm_qa_eks_perms.json

  tags = merge(local.tags, { team = "pmm" })
}

# Surface the role ARN for the `role-to-assume` input of
# `aws-actions/configure-aws-credentials` in the pmm-qa workflows. Role ARNs
# are not secrets; a plain output avoids console hand-copying.
output "gha_pmm_qa_eks_role_arn" {
  description = "role-to-assume for percona/pmm-qa GHA workflows connecting to the pmm-ha* QA EKS clusters."
  value       = module.gha_pmm_qa_eks.role_arn
}
