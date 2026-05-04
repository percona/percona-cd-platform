# Cluster VPC. CIDR avoids the Jenkins-VPC ranges (var.vpc_cidr default 10.220.0.0/16).
# Subnets carry the EKS LB-controller tags (kubernetes.io/role/elb, internal-elb)
# and the Karpenter discovery tag.
#
# Hardening (docs/eks-hardening.md):
#  11. S3 gateway VPC endpoint is provisioned alongside (free; drops NAT-GW egress
#      for ECR-pull object reads + Helm chart downloads). Interface endpoints
#      (ECR, STS, Secrets Manager) are paid; revisit when NAT-GW bill warrants.

module "vpc" {
  source  = local.modules.vpc.source
  version = local.modules.vpc.version

  name = "${local.cluster_name}-vpc"
  cidr = var.vpc_cidr

  azs             = ["${var.aws_region}a", "${var.aws_region}b", "${var.aws_region}c"]
  private_subnets = ["10.220.0.0/19", "10.220.32.0/19", "10.220.64.0/19"]
  public_subnets  = ["10.220.96.0/24", "10.220.97.0/24", "10.220.98.0/24"]

  enable_nat_gateway   = true
  single_nat_gateway   = true # one shared NAT — see docs/eks-hardening.md (deferred multi-AZ)
  enable_dns_hostnames = true
  enable_dns_support   = true

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

# S3 gateway endpoint — free; routes private-subnet S3 traffic via the VPC route
# table instead of NAT. Used for ECR pulls (which fetch object data over S3) and
# Helm chart downloads from S3-backed registries.
module "vpc_endpoints" {
  source  = local.modules.vpc_endpoints.source
  version = local.modules.vpc_endpoints.version

  vpc_id = module.vpc.vpc_id

  endpoints = {
    s3 = {
      service         = "s3"
      service_type    = "Gateway"
      route_table_ids = module.vpc.private_route_table_ids
      tags            = { Name = "${local.cluster_name}-s3-gw-endpoint" }
    }
  }

  tags = local.tags
}
