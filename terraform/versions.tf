# Source of truth for OpenTofu engine + provider + module + Helm chart versions.
# Bumping a pin here is the only edit needed to roll a new version.
# Verify pins programmatically before merge: `just check-versions`.

terraform {
  required_version = ">= 1.11.0, < 2.0.0" # OpenTofu 1.11.x

  required_providers {
    aws        = { source = "hashicorp/aws", version = "~> 6.43" }
    kubernetes = { source = "hashicorp/kubernetes", version = "~> 2.35" }
    helm       = { source = "hashicorp/helm", version = "~> 2.17" }
    kubectl    = { source = "alekc/kubectl", version = "~> 2.1" }
    tls        = { source = "hashicorp/tls", version = "~> 4.0" }
  }
}

# Module source/version map. Every module block references local.modules.<x>.{source,version}.
# OpenTofu 1.8+ allows variable interpolation in module.source/version (early eval).
locals {
  modules = {
    vpc           = { source = "terraform-aws-modules/vpc/aws", version = "6.6.1" }
    vpc_endpoints = { source = "terraform-aws-modules/vpc/aws//modules/vpc-endpoints", version = "6.6.1" }
    eks           = { source = "terraform-aws-modules/eks/aws", version = "21.19.0" }
    karpenter     = { source = "terraform-aws-modules/eks/aws//modules/karpenter", version = "21.19.0" }
    pod_identity  = { source = "terraform-aws-modules/eks-pod-identity/aws", version = "2.8.0" }
    iam_irsa      = { source = "terraform-aws-modules/iam/aws//modules/iam-role-for-service-accounts-eks", version = "6.6.0" }
    acm           = { source = "terraform-aws-modules/acm/aws", version = "6.3.0" }
  }

  # Helm chart pins. ArgoCD Applications template these via cluster-secret annotations.
  charts = {
    argo_cd = {
      repo = "https://argoproj.github.io/argo-helm"
      name = "argo-cd"
      ver  = "9.5.9"
    }
    aws_load_balancer_controller = {
      repo = "https://aws.github.io/eks-charts"
      name = "aws-load-balancer-controller"
      ver  = "3.2.2"
    }
    external_dns = {
      repo = "https://kubernetes-sigs.github.io/external-dns/"
      name = "external-dns"
      ver  = "1.21.1"
    }
    karpenter = {
      repo = "oci://public.ecr.aws/karpenter"
      name = "karpenter"
      ver  = "1.12.0"
    }
    kube_prometheus_stack = {
      repo = "https://prometheus-community.github.io/helm-charts"
      name = "kube-prometheus-stack"
      ver  = "84.4.0"
    }
    # cert-manager deferred to v1.5 — see docs/adr/0004-pod-identity-default.md (TBC) and the plan.
    # cert_manager = { repo = "https://charts.jetstack.io", name = "cert-manager", ver = "v1.20.2" }
  }
}
