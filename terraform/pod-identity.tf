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
      # Namespace matches the ApplicationSet path basename
      # (resources/addons/aws-load-balancer-controller/) which the
      # addons ApplicationSet uses for `destination.namespace`. The
      # historical default of `kube-system` (which the eks-charts AWS LBC
      # docs assume) does not match this repo's GitOps layout, so the
      # association would never bind and the controller falls back to
      # IMDS on every AWS API call (ACM, EC2, ELBv2 …).
      namespace       = "aws-load-balancer-controller"
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

  # The upstream `attach_external_secrets_policy` preset grants only
  # kms:Decrypt — not secretsmanager:GetSecretValue. So ESO can decrypt
  # but cannot actually fetch secrets, and every ExternalSecret reconcile
  # fails with AccessDeniedException. This extra policy fills the gap,
  # scoped to the cluster's prefix (percona-ci-platform/*).
  additional_policy_arns = {
    secrets_read = aws_iam_policy.eso_secrets_manager_read.arn
  }

  associations = {
    main = {
      cluster_name    = module.eks.cluster_name
      namespace       = "external-secrets"
      service_account = "external-secrets"
    }
  }

  tags = local.tags
}

# Inline-shaped Secrets Manager + SSM read policy for ESO. Scoped to the
# cluster's prefix (`${local.cluster_name}/*`) so adding new secrets
# under that path is a no-op IAM-wise. Tightening to per-app prefixes
# is a path-edit if/when warranted.
data "aws_iam_policy_document" "eso_secrets_manager_read" {
  statement {
    sid = "SecretsManagerRead"
    actions = [
      "secretsmanager:GetSecretValue",
      "secretsmanager:DescribeSecret",
      "secretsmanager:ListSecrets",
    ]
    resources = [
      "arn:aws:secretsmanager:${var.aws_region}:${data.aws_caller_identity.current.account_id}:secret:${local.cluster_name}/*",
    ]
  }
}

resource "aws_iam_policy" "eso_secrets_manager_read" {
  name        = "${local.cluster_name}-eso-secrets-read"
  description = "External-Secrets-Operator AWS Secrets Manager read for ${local.cluster_name}/*"
  policy      = data.aws_iam_policy_document.eso_secrets_manager_read.json
  tags        = local.tags
}

# ---------------------------------------------------------------------------
# LGTM components — Mimir / Loki / Tempo. No built-in policy preset for these
# in the upstream pod-identity module, so each component gets a hand-rolled
# IAM policy scoped to its own bucket (lgtm-storage.tf) + the shared LGTM CMK.
# ---------------------------------------------------------------------------

data "aws_iam_policy_document" "lgtm_component" {
  for_each = local.lgtm_buckets

  statement {
    sid       = "BucketLevel"
    actions   = ["s3:ListBucket", "s3:GetBucketLocation"]
    resources = [aws_s3_bucket.lgtm[each.key].arn]
  }

  statement {
    sid = "ObjectLevel"
    actions = [
      "s3:GetObject",
      "s3:PutObject",
      "s3:DeleteObject",
      "s3:AbortMultipartUpload",
      "s3:ListMultipartUploadParts",
    ]
    resources = ["${aws_s3_bucket.lgtm[each.key].arn}/*"]
  }

  # KMS grants for the shared LGTM CMK. SSE-KMS requires both Encrypt
  # (for puts) and Decrypt (for gets); GenerateDataKey is what S3 actually
  # calls under the hood when bucket_key_enabled is on.
  statement {
    sid = "KmsForObjects"
    actions = [
      "kms:Decrypt",
      "kms:Encrypt",
      "kms:GenerateDataKey",
      "kms:DescribeKey",
    ]
    resources = [aws_kms_key.lgtm.arn]
  }
}

resource "aws_iam_policy" "lgtm_component" {
  for_each = local.lgtm_buckets

  name        = "${local.cluster_name}-${each.key}"
  description = "S3 + KMS access for LGTM ${each.key}, scoped to its bucket only."
  policy      = data.aws_iam_policy_document.lgtm_component[each.key].json

  tags = local.tags
}

module "pod_identity_mimir" {
  source  = local.modules.pod_identity.source
  version = local.modules.pod_identity.version

  name = "${local.cluster_name}-mimir"
  additional_policy_arns = {
    component = aws_iam_policy.lgtm_component["mimir"].arn
  }

  associations = {
    main = {
      cluster_name    = module.eks.cluster_name
      namespace       = "mimir"
      service_account = "mimir"
    }
  }

  tags = local.tags
}

module "pod_identity_loki" {
  source  = local.modules.pod_identity.source
  version = local.modules.pod_identity.version

  name = "${local.cluster_name}-loki"
  additional_policy_arns = {
    component = aws_iam_policy.lgtm_component["loki"].arn
  }

  associations = {
    main = {
      cluster_name    = module.eks.cluster_name
      namespace       = "loki"
      service_account = "loki"
    }
  }

  tags = local.tags
}

module "pod_identity_tempo" {
  source  = local.modules.pod_identity.source
  version = local.modules.pod_identity.version

  name = "${local.cluster_name}-tempo"
  additional_policy_arns = {
    component = aws_iam_policy.lgtm_component["tempo"].arn
  }

  associations = {
    main = {
      cluster_name    = module.eks.cluster_name
      namespace       = "tempo"
      service_account = "tempo"
    }
  }

  tags = local.tags
}
