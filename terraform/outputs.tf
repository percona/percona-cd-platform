# Outputs feed the ArgoCD cluster-secret annotations contract (see argocd.tf when implemented).

output "cluster_name" {
  description = "EKS cluster name."
  value       = module.eks.cluster_name
}

output "cluster_endpoint" {
  description = "EKS API endpoint URL."
  value       = module.eks.cluster_endpoint
}

output "cluster_certificate_authority_data" {
  description = "Base64-encoded cluster CA certificate."
  value       = module.eks.cluster_certificate_authority_data
  sensitive   = true
}

output "cluster_version" {
  description = "Kubernetes minor version actually in use."
  value       = module.eks.cluster_version
}

output "oidc_provider_arn" {
  description = "OIDC provider ARN — used by IRSA fallbacks; Pod Identity does not need it."
  value       = module.eks.oidc_provider_arn
}

output "cluster_security_group_id" {
  description = "Security group attached to the cluster ENIs."
  value       = module.eks.cluster_security_group_id
}

output "node_security_group_id" {
  description = "Security group attached to managed-NG worker ENIs."
  value       = module.eks.node_security_group_id
}

output "kms_key_arn" {
  description = "Customer-managed CMK used for cluster-secret envelope encryption."
  value       = module.eks.kms_key_arn
}

output "vpc_id" {
  description = "Cluster VPC ID."
  value       = module.vpc.vpc_id
}

output "private_subnets" {
  description = "Private subnet IDs (managed NGs + Karpenter discovery)."
  value       = module.vpc.private_subnets
}

output "public_subnets" {
  description = "Public subnet IDs (ALB)."
  value       = module.vpc.public_subnets
}

# Downstream waves will append:
# - acm_wildcard_arn (acm.tf)
# - karpenter_sqs_name, karpenter_node_role_arn (karpenter-prereqs.tf)
# - <addon>_role_arn for each Pod Identity association (pod-identity.tf)
