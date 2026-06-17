# Owner: platform
# Regional EFS filesystem backing the aws-efs-csi-driver (eks-addons.tf). EFS
# is the only ReadWriteMany / multi-AZ storage on the platform — EBS is per-AZ
# and single-writer, so any workload that needs a shared volume across replicas
# or AZs (the documented prerequisite for Grafana HA, see ADR 0008 and
# docs/eks-hardening.md) provisions against the efs-sc StorageClass
# (resources/addons/storageclass-efs/), which dynamically carves an EFS access
# point per PVC under this filesystem.
#
# Mount targets are placed in every private subnet (one per AZ): the ingress
# Karpenter NodePool spans 1a/1b/1c, so a pod needing EFS can land in any AZ
# and must reach an in-AZ mount target. Throughput is Elastic (pay per GB moved,
# no burst-credit cliff on a shared filesystem). Encryption at rest uses the
# cluster KMS CMK; encryption in transit (TLS) is on by default in the driver.

resource "aws_efs_file_system" "platform" {
  creation_token   = "${local.cluster_name}-platform"
  encrypted        = true
  kms_key_id       = module.eks.kms_key_arn
  throughput_mode  = "elastic"
  performance_mode = "generalPurpose"

  tags = merge(local.tags, {
    Name = "${local.cluster_name}-efs"
  })
}

# NFS ingress from the cluster CIDR. Pods receive VPC-routable IPs via the
# VPC CNI, so the CIDR covers both node and pod ENIs — same shape as the
# vpc_endpoints SG. EFS mount targets cannot originate traffic, so no egress
# rule is needed beyond the implicit response path.
resource "aws_security_group" "efs" {
  name        = "${local.cluster_name}-efs"
  description = "EFS mount targets - NFS 2049 from cluster CIDR"
  vpc_id      = module.vpc.vpc_id

  ingress {
    description = "NFS from cluster CIDR"
    from_port   = 2049
    to_port     = 2049
    protocol    = "tcp"
    cidr_blocks = [var.vpc_cidr]
  }

  tags = local.tags
}

# One mount target per private subnet (AZ). The CSI node DaemonSet on a given
# node mounts through the mount target in that node's AZ.
resource "aws_efs_mount_target" "platform" {
  for_each = toset(module.vpc.private_subnets)

  file_system_id  = aws_efs_file_system.platform.id
  subnet_id       = each.value
  security_groups = [aws_security_group.efs.id]
}
