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
    # Standalone CRDs — decoupled from kube-prometheus-stack so the
    # monitoring.coreos.com CRDs survive when kps is removed
    # (LGTM-only, ADR 0016). AppVersion v0.90.1 must match the operator
    # image kps ships.
    prometheus_operator_crds = {
      repo = "https://prometheus-community.github.io/helm-charts"
      name = "prometheus-operator-crds"
      ver  = "28.0.1"
    }
    # Standalone KSM and node-exporter — replace the kps subcharts so
    # they survive kps removal (P6). Versions track upstream; verify
    # latest with scripts/check_versions.py before bumping.
    kube_state_metrics = {
      repo = "https://prometheus-community.github.io/helm-charts"
      name = "kube-state-metrics"
      ver  = "7.3.0"
    }
    prometheus_node_exporter = {
      repo = "https://prometheus-community.github.io/helm-charts"
      name = "prometheus-node-exporter"
      ver  = "4.55.0"
    }
    # LGTM stack — distributed-mode pins. Versions verified live via
    # `helm search repo grafana/<chart> --versions | head -2` on 2026-05-06.
    mimir_distributed = {
      repo = "https://grafana.github.io/helm-charts"
      name = "mimir-distributed"
      ver  = "6.0.6"
    }
    loki = {
      repo = "https://grafana.github.io/helm-charts"
      name = "loki"
      ver  = "7.0.0"
    }
    tempo_distributed = {
      repo = "https://grafana.github.io/helm-charts"
      name = "tempo-distributed"
      ver  = "1.61.3"
    }
    grafana = {
      repo = "https://grafana.github.io/helm-charts"
      name = "grafana"
      ver  = "10.5.15"
    }
    alloy = {
      repo = "https://grafana.github.io/helm-charts"
      name = "alloy"
      ver  = "1.8.0"
    }
    # cert-manager deferred to v1.5 — see docs/adr/0004-pod-identity-default.md (TBC) and the plan.
    # cert_manager = { repo = "https://charts.jetstack.io", name = "cert-manager", ver = "v1.20.2" }
  }
}
