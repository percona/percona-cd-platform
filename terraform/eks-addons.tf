# Owner: platform
# Managed EKS addons. Versions deliberately unset on first apply — the EKS API
# resolves to the channel default for var.cluster_version (1.35). Pin via
# var.addon_versions in local.auto.tfvars after first apply, per
# docs/eks-hardening.md item #5 ("never @latest").
#
# eks-pod-identity-agent is mandatory for any Pod Identity association to
# function — without it every association silently no-ops.

resource "aws_eks_addon" "vpc_cni" {
  cluster_name                = module.eks.cluster_name
  addon_name                  = "vpc-cni"
  addon_version               = lookup(var.addon_versions, "vpc-cni", null)
  resolve_conflicts_on_create = "OVERWRITE"
  resolve_conflicts_on_update = "PRESERVE"

  # docs/eks-hardening.md:
  #  #4  ENABLE_PREFIX_DELEGATION → ~4× pod density (prefix mode) on the Graviton MNG nodes
  #  #16 enableNetworkPolicy = true → native VPC-CNI L3/L4 NetworkPolicy engine
  #      available; default-deny baselines can ship later as Kubernetes
  #      NetworkPolicy manifests.
  configuration_values = jsonencode({
    env = {
      ENABLE_PREFIX_DELEGATION = "true"
      WARM_PREFIX_TARGET       = "1"
    }
    enableNetworkPolicy = "true"
  })

  tags = local.tags
}

resource "aws_eks_addon" "kube_proxy" {
  cluster_name                = module.eks.cluster_name
  addon_name                  = "kube-proxy"
  addon_version               = lookup(var.addon_versions, "kube-proxy", null)
  resolve_conflicts_on_create = "OVERWRITE"
  resolve_conflicts_on_update = "PRESERVE"
  tags                        = local.tags
}

resource "aws_eks_addon" "coredns" {
  cluster_name                = module.eks.cluster_name
  addon_name                  = "coredns"
  addon_version               = lookup(var.addon_versions, "coredns", null)
  resolve_conflicts_on_create = "OVERWRITE"
  resolve_conflicts_on_update = "PRESERVE"

  # CoreDNS pods land on the system NG only — multi-AZ on-demand, no spot interrupts.
  configuration_values = jsonencode({
    nodeSelector = { "workload.percona.com/tier" = "bootstrap" }
    replicaCount = 2
  })

  tags = local.tags
}

resource "aws_eks_addon" "pod_identity_agent" {
  cluster_name                = module.eks.cluster_name
  addon_name                  = "eks-pod-identity-agent"
  addon_version               = lookup(var.addon_versions, "eks-pod-identity-agent", null)
  resolve_conflicts_on_create = "OVERWRITE"
  resolve_conflicts_on_update = "PRESERVE"
  tags                        = local.tags
}

resource "aws_eks_addon" "ebs_csi_driver" {
  cluster_name                = module.eks.cluster_name
  addon_name                  = "aws-ebs-csi-driver"
  addon_version               = lookup(var.addon_versions, "aws-ebs-csi-driver", null)
  resolve_conflicts_on_create = "OVERWRITE"
  resolve_conflicts_on_update = "PRESERVE"

  # EBS-CSI controller pins to the system NG; the node DaemonSet is unrestricted.
  # extraVolumeTags is the backstop for any future StorageClass added without
  # parameters.tagSpecification_*: every CSI-provisioned volume gets the reaper
  # pair (docs/runbooks/cleanup-reapers.md) even if the class forgets it.
  configuration_values = jsonencode({
    controller = {
      nodeSelector = { "workload.percona.com/tier" = "bootstrap" }
      replicaCount = 2
      extraVolumeTags = {
        "iit-billing-tag" = "percona-ci-platform"
        "PerconaKeep"     = "True"
        "managed-by"      = "ebs-csi-driver"
        "team"            = "platform"
      }
    }
  })

  # Pod Identity association is created in pod-identity.tf; depends_on keeps
  # the addon from racing the agent.
  depends_on = [
    aws_eks_addon.pod_identity_agent,
    module.pod_identity_ebs_csi,
  ]

  tags = local.tags
}

resource "aws_eks_addon" "efs_csi_driver" {
  cluster_name                = module.eks.cluster_name
  addon_name                  = "aws-efs-csi-driver"
  addon_version               = lookup(var.addon_versions, "aws-efs-csi-driver", null)
  resolve_conflicts_on_create = "OVERWRITE"
  resolve_conflicts_on_update = "PRESERVE"

  # Controller pins to the system NG (multi-AZ on-demand, no spot interrupts),
  # matching the EBS-CSI and CoreDNS placement. The node DaemonSet stays
  # unrestricted — it must run on every node to perform the NFS mount. Dynamic
  # provisioning creates an EFS access point per PVC (not an EBS volume), so the
  # extraVolumeTags backstop the EBS driver carries does not apply here; the
  # filesystem itself is tagged in efs.tf.
  configuration_values = jsonencode({
    controller = {
      nodeSelector = { "workload.percona.com/tier" = "bootstrap" }
      replicaCount = 2
    }
  })

  # Pod Identity association is created in pod-identity.tf; depends_on keeps
  # the addon from racing the agent.
  depends_on = [
    aws_eks_addon.pod_identity_agent,
    module.pod_identity_efs_csi,
  ]

  tags = local.tags
}

resource "aws_eks_addon" "snapshot_controller" {
  cluster_name                = module.eks.cluster_name
  addon_name                  = "snapshot-controller"
  addon_version               = lookup(var.addon_versions, "snapshot-controller", null)
  resolve_conflicts_on_create = "OVERWRITE"
  resolve_conflicts_on_update = "PRESERVE"

  # CSI snapshot controller (kubernetes-csi/external-snapshotter) plus the
  # snapshot.storage.k8s.io CRDs. AWS recommends the managed add-on over a
  # self-managed install; it bundles the CRDs and handles controller placement,
  # so no vendored Deployment/RBAC/CRDs are needed. Requires a CSI driver with
  # snapshot support (the EBS CSI add-on above). The VolumeSnapshotClass is a
  # user resource added via GitOps when a consumer (the k8s pilot) needs it.
  depends_on = [aws_eks_addon.ebs_csi_driver]

  tags = local.tags
}
