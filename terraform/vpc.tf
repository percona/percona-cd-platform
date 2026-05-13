# Cluster VPC. CIDR avoids the Jenkins-VPC ranges (var.vpc_cidr default 10.220.0.0/16).
# Subnets carry the EKS LB-controller tags (kubernetes.io/role/elb, internal-elb)
# and the Karpenter discovery tag.
#
# Hardening (docs/eks-hardening.md):
#  11. S3 gateway VPC endpoint is provisioned alongside (free; drops NAT-GW egress
#      for ECR-pull object reads + Helm chart downloads). Interface endpoints
#      (ECR, STS, Secrets Manager) are paid; revisit when NAT-GW bill warrants.
#
# Single-AZ collapse (2026-05): one NAT-GW in 1a only. Cluster workloads
# all live in 1a now (Karpenter NodePools + LGTM single-AZ + prometheus_system
# pinned). The S3 gateway endpoint absorbs the bulk of egress (ECR pulls);
# remaining NAT traffic is negligible (~0.4 MB / 30d observed across the
# three NATs before collapse). Saves ~$64/mo idle.

module "vpc" {
  source  = local.modules.vpc.source
  version = local.modules.vpc.version

  name = "${local.cluster_name}-vpc"
  cidr = var.vpc_cidr

  azs             = ["${var.aws_region}a", "${var.aws_region}b", "${var.aws_region}c"]
  private_subnets = ["10.220.0.0/19", "10.220.32.0/19", "10.220.64.0/19"]
  public_subnets  = ["10.220.96.0/24", "10.220.97.0/24", "10.220.98.0/24"]

  enable_nat_gateway     = true
  single_nat_gateway     = true # single shared NAT in 1a (single-AZ collapse)
  one_nat_gateway_per_az = false
  enable_dns_hostnames   = true
  enable_dns_support     = true

  public_subnet_tags = {
    "kubernetes.io/role/elb"                      = 1
    "kubernetes.io/cluster/${local.cluster_name}" = "shared"
  }
  private_subnet_tags = {
    "kubernetes.io/role/internal-elb"             = 1
    "kubernetes.io/cluster/${local.cluster_name}" = "shared"
    "karpenter.sh/discovery"                      = local.cluster_name
  }

  tags = local.tags
}

# Security group for VPC interface endpoints — allows HTTPS (443) from the
# cluster CIDR. Single SG attached to all interface endpoints below.
resource "aws_security_group" "vpc_endpoints" {
  name        = "${local.cluster_name}-vpc-endpoints"
  description = "VPC interface endpoints - HTTPS from cluster CIDR"
  vpc_id      = module.vpc.vpc_id

  ingress {
    description = "HTTPS from cluster CIDR"
    from_port   = 443
    to_port     = 443
    protocol    = "tcp"
    cidr_blocks = [var.vpc_cidr]
  }

  egress {
    description = "Allow all egress (responses + nothing else originates here)"
    from_port   = 0
    to_port     = 0
    protocol    = "-1"
    cidr_blocks = ["0.0.0.0/0"]
  }

  tags = local.tags
}

# VPC endpoints — S3 gateway (free) plus the high-leverage interface
# endpoints that pods + Karpenter call constantly.
#
# Interface endpoints add ~$22/mo each (3-AZ × $7.30) of idle cost. Selected:
#   - ecr_api / ecr_dkr — image pulls (scaling-critical)
#   - sts             — Pod Identity token refresh (constant load)
#   - ec2             — Karpenter SDK (scaling-critical)
#
# Long-tail APIs (SQS, Secrets Manager, KMS, ELB, SSM, Logs) deliberately
# excluded — low traffic for now; revisit when NAT-GW data bill warrants.
module "vpc_endpoints" {
  source  = local.modules.vpc_endpoints.source
  version = local.modules.vpc_endpoints.version

  vpc_id             = module.vpc.vpc_id
  security_group_ids = [aws_security_group.vpc_endpoints.id]

  endpoints = {
    # Gateway endpoints — free, route-table-based.
    s3 = {
      service         = "s3"
      service_type    = "Gateway"
      route_table_ids = module.vpc.private_route_table_ids
      tags            = { Name = "${local.cluster_name}-s3-gw" }
    }
    # Interface endpoints — paid per-AZ, but kill NAT-GW egress for the
    # constant-load APIs and survive AZ outages independently.
    ecr_api = {
      service             = "ecr.api"
      service_type        = "Interface"
      subnet_ids          = module.vpc.private_subnets
      private_dns_enabled = true
      tags                = { Name = "${local.cluster_name}-ecr-api" }
    }
    ecr_dkr = {
      service             = "ecr.dkr"
      service_type        = "Interface"
      subnet_ids          = module.vpc.private_subnets
      private_dns_enabled = true
      tags                = { Name = "${local.cluster_name}-ecr-dkr" }
    }
    sts = {
      service             = "sts"
      service_type        = "Interface"
      subnet_ids          = module.vpc.private_subnets
      private_dns_enabled = true
      tags                = { Name = "${local.cluster_name}-sts" }
    }
    ec2 = {
      service             = "ec2"
      service_type        = "Interface"
      subnet_ids          = module.vpc.private_subnets
      private_dns_enabled = true
      tags                = { Name = "${local.cluster_name}-ec2" }
    }
  }

  tags = local.tags
}
