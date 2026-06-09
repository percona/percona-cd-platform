# Owner: platform
# Karpenter prerequisites (controller IAM role + SQS interruption queue +
# Pod Identity association + node-side IAM role for the EC2NodeClass to assume).
# The controller chart, NodePool, and EC2NodeClass manifests live in
# resources/addons/karpenter/ and are applied by ArgoCD.
#
# Hardening (docs/eks-hardening.md):
#   #5  Pin the controller chart version (versions.tf -> local.charts.karpenter.ver
#       = "1.12.0", consumed by the ArgoCD Application). Pin the EC2NodeClass
#       AMI alias separately in resources/addons/karpenter/nodepools/ec2nodeclass.yaml
#       (wave 5).

module "karpenter" {
  source  = local.modules.karpenter.source
  version = local.modules.karpenter.version

  cluster_name = module.eks.cluster_name

  # Pod Identity (not IRSA) per ADR 0004. v21 of the submodule defaults
  # `create_pod_identity_association = true`; explicit here for documentation.
  # Karpenter v1 permission set is the default in v21 too — no separate flag.
  create_pod_identity_association = true
  namespace                       = "karpenter"
  service_account                 = "karpenter"

  # The Karpenter v1 controller policy is ~7 KiB, which exceeds the default
  # IAM managed-policy length quota of 6,144 chars (L-ED111B8C — not
  # adjustable via Service Quotas, only via AWS Support ticket).
  # `enable_inline_policy = true` switches to an inline IAM role policy
  # whose limit is 10,240 chars — fits comfortably with no support ask.
  # Trade-off: inline policies aren't reusable across roles and don't
  # appear in `aws iam list-policies` — fine here since the policy is
  # 1:1 with the controller role.
  enable_inline_policy = true

  # Stable name for the worker IAM role so the EC2NodeClass instanceProfile
  # reference doesn't churn on rolls.
  node_iam_role_use_name_prefix   = false
  node_iam_role_name              = "${local.cluster_name}-karpenter-node"
  node_iam_role_attach_cni_policy = true

  # SQS interruption queue (spot termination notices, scheduled-event
  # rebalance recommendations). AWS-managed SSE — interruption events carry
  # no sensitive payload, so SQS-owned keys are sufficient and avoid an
  # extra KMS grant on the cluster CMK.
  queue_name                = "${local.cluster_name}-karpenter"
  queue_managed_sse_enabled = true

  tags = local.tags
}
