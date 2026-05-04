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

  # Stable name for the worker IAM role so the EC2NodeClass instanceProfile
  # reference doesn't churn on rolls.
  node_iam_role_use_name_prefix   = false
  node_iam_role_name              = "${local.cluster_name}-karpenter-node"
  node_iam_role_attach_cni_policy = true

  # SQS interruption queue (spot termination notices, scheduled-event
  # rebalance recommendations). Encrypt with the cluster's KMS CMK.
  queue_name                = "${local.cluster_name}-karpenter"
  queue_managed_sse_enabled = true

  tags = local.tags
}
