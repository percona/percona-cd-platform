# ps3.cd.percona.com -- Terraform-managed via ./modules/jenkins-master/.
# Original CFN stack jenkins-ps3 (Percona-Lab/jenkins-pipelines/IaC/ps3.cd)
# was imported in 2026-05 (PS-10945, plan validated-herding-stardust.md)
# and deleted on 2026-05-19 (PS-11173 Phase 0) via update-stack with
# DeletionPolicy: Retain on all 24 resources, then plain delete-stack.
# All resources are now Terraform-managed; no CFN metadata remains.
#
# EIP eipalloc-081a07ed2e87a8f74 / 52.210.70.55 is still attached to the
# running EC2 instance but is not owned by either CFN or Terraform. The
# userdata's setup_aws() associates it; release is deferred until the
# userdata is updated to drop EIP association (Phase 3 follow-up).

module "ps3" {
  source    = "./modules/jenkins-master"
  providers = { aws = aws.eu-west-1 }

  hostname                = "ps3.cd.percona.com"
  short_name              = "jenkins-ps3"
  vpc_cidr                = "10.181.0.0/22"
  ami_id                  = "ami-0c27e7b62088f2235" # Amazon Linux 2023 in eu-west-1 (matches CF Mapping)
  spot_instance_types     = ["c7i-flex.xlarge", "m7i-flex.xlarge", "c5a.xlarge", "c5d.xlarge"]
  spot_price              = "0.17"
  master_profile          = "eks_observability"
  jenkins_package_version = "2.541.3"
  cache_bucket_name       = "ps-build-cache"
  create_eip              = false # ALB owns HTTPS, SSM owns SSH
  create_route53_record   = false # external-dns owns ps3.cd.percona.com

  # CloudWatch policy is vestigial after the Alloy push migration; kept
  # attached to keep import diff zero, detached in a follow-up.
  extra_master_managed_policies = [
    "arn:aws:iam::aws:policy/CloudWatchAgentServerPolicy",
    "arn:aws:iam::${data.aws_caller_identity.current.account_id}:policy/ec2.DescribeSpotPriceHistory",
  ]

  extra_master_inline_policies = [
    {
      name = "AlloyGatewayBearerRead"
      json = data.aws_iam_policy_document.ps3_alloy_bearer_read.json
    },
    {
      # PS-11173 Phase 3: lets the userdata graceful spot-interrupt drain
      # script fetch the api-admin Jenkins token at boot.
      name = "AdminApiTokenRead"
      json = data.aws_iam_policy_document.ps3_admin_token_read.json
    },
  ]

  # :8080 from EKS VPC over cross-region peering for jenkins-ingress nginx.
  extra_http_ingress = [
    { port = 8080, cidr = module.vpc.vpc_cidr_block },
  ]

  ssh_key_engineers = [
    "anderson.nogueira",
    "alex.miroshnychenko",
    "andrew.siemen",
    "eduardo.casarero",
    "evgeniy.patlan",
    "santiago.ruiz",
    "surabhi.bhat",
    "talha.rizwan",
    "vadim.yalovets",
  ]
}

# PS-11179: ARM Graviton spot fleet for the ec2-fleet plugin -- the
# docker-aarch64 fallback when Hetzner CAX capacity is unavailable.
# capacity-optimized across m8g/m7g/m6g.2xlarge (SPS ~9 vs ~3 single-type).
# Point ps3's EC2FleetCloud at module.ps3_arm_fleet.asg_name on the
# docker-32gb-aarch64 label.
module "ps3_arm_fleet" {
  source    = "./modules/jenkins-arm-fleet"
  providers = { aws = aws.eu-west-1 }

  short_name                   = "jenkins-ps3"
  vpc_id                       = module.ps3.vpc_id
  subnet_ids                   = module.ps3.subnet_ids
  worker_instance_profile_name = module.ps3.worker_instance_profile_name
  master_role_name             = module.ps3.master_iam_role_name
  key_name                     = "percona-jenkins"
  instance_types               = ["m8g.2xlarge", "m7g.2xlarge", "m6g.2xlarge"]
  max_size                     = 16
  tickets                      = "PS-11179"
}

# Rendered in the root so the ARN uses this account's caller-identity, not
# the eu-west-1 alias's. Secret lives in us-east-1 with the rest of alloy-gateway.
data "aws_iam_policy_document" "ps3_alloy_bearer_read" {
  statement {
    sid       = "AlloyGatewayBearerRead"
    effect    = "Allow"
    actions   = ["secretsmanager:GetSecretValue"]
    resources = ["arn:aws:secretsmanager:us-east-1:${data.aws_caller_identity.current.account_id}:secret:percona-ci-platform/alloy-gateway/bearer-*"]
  }
}

# PS-11173 Phase 3: secret holds the Jenkins admin API token consumed by
# the userdata graceful spot-interrupt drain script. Lives in eu-west-1
# alongside the ps3 master so the userdata's $INSTANCE_REGION lookup just
# works without a region override.
data "aws_iam_policy_document" "ps3_admin_token_read" {
  statement {
    sid       = "AdminApiTokenRead"
    effect    = "Allow"
    actions   = ["secretsmanager:GetSecretValue"]
    resources = ["arn:aws:secretsmanager:eu-west-1:${data.aws_caller_identity.current.account_id}:secret:ps3.cd/jenkins/admin-api-token-*"]
  }
}

# Cross-region VPC peering EKS (us-east-1) <-> ps3 (eu-west-1).
# Folded in from the old peering-ps3.tf; state-mv keeps them in place.

resource "aws_vpc_peering_connection" "ps3" {
  vpc_id      = module.vpc.vpc_id
  peer_vpc_id = module.ps3.vpc_id
  peer_region = "eu-west-1"
  auto_accept = false

  tags = {
    Name = "${local.cluster_name}-to-jenkins-ps3"
  }
}

resource "aws_vpc_peering_connection_accepter" "ps3" {
  provider                  = aws.eu-west-1
  vpc_peering_connection_id = aws_vpc_peering_connection.ps3.id
  auto_accept               = true

  tags = {
    Name = "${local.cluster_name}-to-jenkins-ps3"
  }
}

resource "aws_route" "eks_private_to_ps3" {
  for_each                  = toset(module.vpc.private_route_table_ids)
  route_table_id            = each.value
  destination_cidr_block    = module.ps3.vpc_cidr
  vpc_peering_connection_id = aws_vpc_peering_connection.ps3.id
}

resource "aws_route" "ps3_to_eks" {
  provider                  = aws.eu-west-1
  for_each                  = toset(module.ps3.private_route_table_ids)
  route_table_id            = each.value
  destination_cidr_block    = module.vpc.vpc_cidr_block
  vpc_peering_connection_id = aws_vpc_peering_connection.ps3.id
}

