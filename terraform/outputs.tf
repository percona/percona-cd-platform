# Owner: platform
# Outputs feed the ArgoCD cluster-secret annotations contract (see argocd.tf).

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

output "aws_account_id" {
  description = "AWS account ID; used in ArgoCD cluster-secret annotations."
  value       = data.aws_caller_identity.current.account_id
}

output "route53_zone_id" {
  description = "Public hosted zone ID for var.route53_zone_name."
  value       = local.route53_zone_id
}

# Karpenter (karpenter-prereqs.tf)
output "karpenter_node_iam_role_arn" {
  description = "IAM role assumed by Karpenter-launched workers (referenced by EC2NodeClass)."
  value       = module.karpenter.node_iam_role_arn
}

output "karpenter_node_iam_role_name" {
  description = "IAM role name (some chart values want the name, not the ARN)."
  value       = module.karpenter.node_iam_role_name
}

output "karpenter_queue_name" {
  description = "SQS interruption queue name; goes into the Karpenter Helm values."
  value       = module.karpenter.queue_name
}

output "karpenter_iam_role_arn" {
  description = "Karpenter controller IAM role ARN (Pod Identity-bound)."
  value       = module.karpenter.iam_role_arn
}

# Pod Identity associations (pod-identity.tf) — each *_pod_identity_role_arn is
# what the cluster-secret annotation contract carries into addon Helm valuesObject.
output "alb_controller_pod_identity_role_arn" {
  description = "Pod Identity role ARN for the AWS Load Balancer Controller."
  value       = module.pod_identity_alb_controller.iam_role_arn
}

output "external_dns_pod_identity_role_arn" {
  description = "Pod Identity role ARN for external-dns."
  value       = module.pod_identity_external_dns.iam_role_arn
}

output "ebs_csi_pod_identity_role_arn" {
  description = "Pod Identity role ARN for the EBS CSI controller."
  value       = module.pod_identity_ebs_csi.iam_role_arn
}

output "external_secrets_pod_identity_role_arn" {
  description = "Pod Identity role ARN for the External Secrets Operator."
  value       = module.pod_identity_external_secrets.iam_role_arn
}

# ACM (acm.tf)
output "acm_wildcard_arn" {
  description = "Wildcard *.cd.percona.com ACM certificate ARN (referenced by ALB Ingresses)."
  value       = module.acm.acm_certificate_arn
}

# ArgoCD (argocd.tf)
output "argocd_namespace" {
  description = "Namespace where the ArgoCD HA install runs."
  value       = helm_release.argocd.namespace
}

output "argocd_hostname" {
  description = "Public hostname for the ArgoCD UI (resolved by external-dns to the shared ALB)."
  value       = var.argocd_hostname
}

# Origins (origins.tf)
output "jenkins_origin_records" {
  description = "FQDNs of all created origin-<host>.cd.percona.com records (Mode B proxy upstreams)."
  value       = [for h, _ in local.jenkins_origin_records : "origin-${h}.${var.route53_zone_name}"]
}

# Backup (backup.tf)
output "backup_vault_arn" {
  description = "AWS Backup vault ARN holding daily snapshots of stateful PVCs."
  value       = aws_backup_vault.main.arn
}

output "backup_kms_key_arn" {
  description = "CMK encrypting the backup vault. Required to restore in another account/region."
  value       = aws_kms_key.backup.arn
}

# LGTM storage (lgtm-storage.tf)
output "mimir_bucket_name" {
  description = "S3 bucket holding Mimir blocks."
  value       = aws_s3_bucket.lgtm["mimir"].bucket
}

output "loki_bucket_name" {
  description = "S3 bucket holding Loki chunks."
  value       = aws_s3_bucket.lgtm["loki"].bucket
}

output "tempo_bucket_name" {
  description = "S3 bucket holding Tempo traces."
  value       = aws_s3_bucket.lgtm["tempo"].bucket
}

output "lgtm_kms_key_arn" {
  description = "Shared CMK encrypting all three LGTM buckets."
  value       = aws_kms_key.lgtm.arn
}

output "mimir_pod_identity_role_arn" {
  description = "Pod Identity role ARN for Mimir (S3 + KMS scoped to its bucket)."
  value       = module.pod_identity_mimir.iam_role_arn
}

output "loki_pod_identity_role_arn" {
  description = "Pod Identity role ARN for Loki (S3 + KMS scoped to its bucket)."
  value       = module.pod_identity_loki.iam_role_arn
}

output "tempo_pod_identity_role_arn" {
  description = "Pod Identity role ARN for Tempo (S3 + KMS scoped to its bucket)."
  value       = module.pod_identity_tempo.iam_role_arn
}
