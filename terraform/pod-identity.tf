# Five Pod Identity associations for the in-cluster addons that need AWS IAM:
#
#   alb-controller        kube-system / aws-load-balancer-controller
#   external-dns          external-dns / external-dns
#   ebs-csi               kube-system / ebs-csi-controller-sa
#   external-secrets      external-secrets / external-secrets
#   karpenter             handled by karpenter-prereqs.tf (the eks//modules/karpenter
#                         submodule creates its own association inline)
#
# cert-manager is intentionally absent — see ADR 0007 (deferred to v1.5).
# Each block uses the pod-identity module's built-in `attach_<addon>_policy`
# flag, which generates the right AWS-managed policy for the addon. Hand-rolled
# policies are reserved for cases that don't fit the canonical pattern.
#
# Hardening (docs/eks-hardening.md): Pod Identity replaces IRSA for every
# in-cluster IAM consumer here, per ADR 0004. The `eks-pod-identity-agent`
# managed addon (eks-addons.tf) must be running for any of these to function.

module "pod_identity_alb_controller" {
  source  = local.modules.pod_identity.source
  version = local.modules.pod_identity.version

  name                            = "${local.cluster_name}-alb-controller"
  attach_aws_lb_controller_policy = true

  associations = {
    main = {
      cluster_name    = module.eks.cluster_name
      namespace       = "kube-system"
      service_account = "aws-load-balancer-controller"
    }
  }

  tags = local.tags
}

module "pod_identity_external_dns" {
  source  = local.modules.pod_identity.source
  version = local.modules.pod_identity.version

  name                       = "${local.cluster_name}-external-dns"
  attach_external_dns_policy = true
  # Scope external-dns to the platform's public hosted zone only — it cannot
  # then mutate any other zone in the account.
  external_dns_hosted_zone_arns = [
    "arn:aws:route53:::hostedzone/${local.route53_zone_id}",
  ]

  associations = {
    main = {
      cluster_name    = module.eks.cluster_name
      namespace       = "external-dns"
      service_account = "external-dns"
    }
  }

  tags = local.tags
}

module "pod_identity_ebs_csi" {
  source  = local.modules.pod_identity.source
  version = local.modules.pod_identity.version

  name                      = "${local.cluster_name}-ebs-csi"
  attach_aws_ebs_csi_policy = true
  # EBS-CSI volumes are encrypted with the cluster KMS CMK; grant the
  # controller role explicit permission against that specific key.
  aws_ebs_csi_kms_arns = [module.eks.kms_key_arn]

  associations = {
    main = {
      cluster_name    = module.eks.cluster_name
      namespace       = "kube-system"
      service_account = "ebs-csi-controller-sa"
    }
  }

  tags = local.tags
}

module "pod_identity_external_secrets" {
  source  = local.modules.pod_identity.source
  version = local.modules.pod_identity.version

  name                           = "${local.cluster_name}-external-secrets"
  attach_external_secrets_policy = true

  associations = {
    main = {
      cluster_name    = module.eks.cluster_name
      namespace       = "external-secrets"
      service_account = "external-secrets"
    }
  }

  tags = local.tags
}
