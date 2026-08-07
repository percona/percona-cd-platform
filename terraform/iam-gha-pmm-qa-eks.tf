# Owner: pmm
# GitHub Actions OIDC role for the public `percona/pmm-qa` repo: QA
# workflows build a kubeconfig for the PMM HA EKS clusters (`pmm-ha`,
# `pmm-ha-test-*`, us-east-2).
#
# AWS-side only. Kubernetes authorization is granted by the cluster-creating
# pmm3-ha-eks Jenkins job (Percona-Lab/jenkins-pipelines) via EKS access
# entries at create time; the persistent `pmm-ha` needs that grant once, out
# of band. Trust shape (StringEquals aud + sub, no wildcards) lives in
# ./modules/github-oidc-role.
#
# Public-repo trust boundary:
#   - Fork `pull_request` runs cannot request `id-token: write`, so fork
#     code cannot assume this role.
#   - Same-repo PRs can, so the boundary is push access to pmm-qa, not
#     code merged to main.
#   - `pull_request_target` / `workflow_run` workflows run in the base-repo
#     context, may hold `id-token: write`, and match this allowlist. They
#     must never run fork-controlled code. Audit at introduction: zero
#     `pull_request_target`, one `workflow_run` notifier without `id-token`.
#
# Leaked-token blast radius (STS session up to 1h, the real window):
#   - Direct: eks:DescribeCluster on the two pmm-ha families (returns
#     endpoint + CA, no credentials). No other AWS API, no other cluster.
#   - Via access entries: cluster-admin on the QA clusters, including the
#     persistent `pmm-ha`. That reaches all in-cluster Secrets, privileged
#     pods, and node instance-profile / IRSA credentials, so transitive AWS
#     exposure is bounded by the QA clusters' configuration, not by this
#     policy.

data "aws_iam_policy_document" "gha_pmm_qa_eks_perms" {
  # update-kubeconfig's only required call, name-scoped to the two known
  # cluster families so a future sibling sharing the pmm-ha prefix is
  # excluded.
  statement {
    sid     = "DescribePmmHaClustersOnly"
    effect  = "Allow"
    actions = ["eks:DescribeCluster"]
    resources = [
      "arn:aws:eks:us-east-2:${data.aws_caller_identity.current.account_id}:cluster/pmm-ha",
      "arn:aws:eks:us-east-2:${data.aws_caller_identity.current.account_id}:cluster/pmm-ha-test-*",
    ]
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

# role-to-assume for `aws-actions/configure-aws-credentials` in pmm-qa
# workflows; role ARNs are not secrets.
output "gha_pmm_qa_eks_role_arn" {
  description = "role-to-assume for percona/pmm-qa GHA workflows connecting to the pmm-ha* QA EKS clusters."
  value       = module.gha_pmm_qa_eks.role_arn
}
