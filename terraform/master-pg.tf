# Owner: postgresql
# pg.cd.percona.com -- Terraform-managed via ./modules/jenkins-master/.
# Migrated from CloudFormation stack jenkins-pg
# (Percona-Lab/jenkins-pipelines/IaC/pg.cd), the last CFN master.
#
# Unlike prior cutovers the live VPC is KEPT and imported: QA molecule
# package tests hardcode pg subnet IDs, and the secondary CIDR
# 10.145.0.0/21 (subnets B2/C2) carries every classic EC2 worker and
# molecule VM. The live route table also holds two out-of-band VGW routes
# (corp VPN, handled separately); the module leaves unmanaged routes alone.

module "pg" {
  source    = "./modules/jenkins-master"
  providers = { aws = aws.eu-central-1 }

  hostname                = "pg.cd.percona.com"
  short_name              = "jenkins-pg"
  team                    = "postgresql"
  ami_id                  = nonsensitive(data.aws_ssm_parameter.al2023_minimal_euc1.value) # latest AL2023 minimal (amis.tf)
  master_profile          = "eks_observability"
  ssh_allowed_cidrs       = local.master_ssh_allowed_cidrs
  jenkins_package_version = "2.541.3" # closes CVE-2026-27100 (pg is on 2.528.3)

  # Live network shape. B/C are the module's own cidrsubnet() math; B2/C2
  # ride the secondary block.
  vpc_cidr           = "10.144.0.0/22"
  secondary_vpc_cidr = "10.145.0.0/21"
  extra_worker_subnets = {
    b2 = { az_index = 1, cidr = "10.145.0.0/22" }
    c2 = { az_index = 2, cidr = "10.145.4.0/22" }
  }

  # A, B and C primary subnets enabled, one per AZ.
  extra_subnet_a = true

  # Retained data volume: live 500 GiB gp3 in eu-central-1b (the template
  # still said 100). encrypted=false is covered by the module's
  # ignore_changes.
  ebs_size = 500
  ebs_type = "gp3"

  # On-demand ends the last spot controller. Sized from host metrics
  # (memory peak 6 GiB, CPU peak about half a core); heap stays at the
  # live 4096m, set explicitly.
  purchasing_option       = "on-demand"
  on_demand_instance_type = "c7i-flex.xlarge"
  jvm_memory_opts         = "-Xms3072m -Xmx4096m -Xss4m"

  # Distinct name: the CFN PGMasterTemplate coexists during the cutover.
  launch_template_name = "PGMasterTemplateTF"

  # EIP-less like the fleet: pg has no inbound agents, and DNS flips to
  # the ALB once the CFN record dies with the stack.
  create_eip      = false
  master_key_name = "percona-jenkins"

  # Live worker grants: build-cache S3 here, tag/describe verbs in the
  # module worker policy, DescribeSpotPriceHistory in StartInstances.
  cache_bucket_name = "pg-build-cache"

  extra_master_inline_policies = [
    {
      name = "AlloyGatewayBearerRead"
      json = data.aws_iam_policy_document.pg_alloy_bearer_read.json
    },
  ]

  # :8080 from the EKS VPC over cross-region peering for the
  # jenkins-ingress nginx (TLS offloaded at the ALB).
  extra_http_ingress = [
    { port = 8080, cidr = module.vpc.vpc_cidr_block },
  ]

  ssh_key_engineers = [
    "anderson.nogueira",
    "alex.miroshnychenko",
    "eduardo.casarero",
    "evgeniy.patlan",
    "santiago.ruiz",
    "surabhi.bhat",
    "talha.rizwan",
    "vadim.yalovets",
  ]

  # init.groovy.d via the module S3 bucket, byte-identical to the live
  # EBS copy minus the dead EC2-side repo.ci /etc/hosts pin. netMap
  # subnet IDs are unchanged because the subnets are imported.
  init_groovy_files = {
    for f in fileset("${path.module}/../resources/jenkins-masters/pg/init.groovy.d", "*.groovy") :
    f => file("${path.module}/../resources/jenkins-masters/pg/init.groovy.d/${f}")
  }
  init_groovy_sync_schedule = "rate(30 minutes)"
}

# ARM Graviton spot fleet for the ec2-fleet plugin -- the docker-aarch64
# fallback. Pre-provisioned while pg was CFN-managed; now consumes the TF
# master's outputs (the ASG already spans all four subnets).
module "pg_arm_fleet" {
  source    = "./modules/jenkins-arm-fleet"
  providers = { aws = aws.eu-central-1 }

  short_name                   = "jenkins-pg"
  team                         = "postgresql"
  vpc_id                       = module.pg.vpc_id
  subnet_ids                   = module.pg.subnet_ids
  worker_instance_profile_name = module.pg.worker_instance_profile_name
  master_role_name             = module.pg.master_iam_role_name
  key_name                     = "percona-jenkins"
  instance_types               = ["m8g.2xlarge", "m7g.2xlarge", "m6g.2xlarge", "m7gd.2xlarge", "m6gd.2xlarge", "r8g.2xlarge", "r7g.2xlarge", "r6g.2xlarge"]
  max_size                     = 16
  tickets                      = "PS-11179"
}

# Rendered in the root so the ARN uses this account's caller-identity, not
# the eu-central-1 alias's. Secret lives in us-east-1 with alloy-gateway.
data "aws_iam_policy_document" "pg_alloy_bearer_read" {
  statement {
    sid       = "AlloyGatewayBearerRead"
    effect    = "Allow"
    actions   = ["secretsmanager:GetSecretValue"]
    resources = ["arn:aws:secretsmanager:us-east-1:${data.aws_caller_identity.current.account_id}:secret:percona-ci-platform/alloy-gateway/bearer-*"]
  }
}

# Cross-region VPC peering EKS (us-east-1) <-> pg (eu-central-1) for the
# jenkins-ingress nginx to reach the master on :8080. Only the primary
# CIDR is routed; workers never talk to EKS.
resource "aws_vpc_peering_connection" "pg" {
  vpc_id      = module.vpc.vpc_id
  peer_vpc_id = module.pg.vpc_id
  peer_region = "eu-central-1"
  auto_accept = false

  tags = {
    Name = "${local.cluster_name}-to-jenkins-pg"
  }
}

resource "aws_vpc_peering_connection_accepter" "pg" {
  provider                  = aws.eu-central-1
  vpc_peering_connection_id = aws_vpc_peering_connection.pg.id
  auto_accept               = true

  tags = {
    Name = "${local.cluster_name}-to-jenkins-pg"
  }
}

resource "aws_route" "eks_private_to_pg" {
  for_each                  = toset(module.vpc.private_route_table_ids)
  route_table_id            = each.value
  destination_cidr_block    = module.pg.vpc_cidr
  vpc_peering_connection_id = aws_vpc_peering_connection.pg.id
}

# Statically-keyed map (not toset over the IDs): the route-table ID is
# unknown at plan time on a fresh build, and the module exposes exactly
# one route table.
resource "aws_route" "pg_to_eks" {
  provider                  = aws.eu-central-1
  for_each                  = { main = one(module.pg.private_route_table_ids) }
  route_table_id            = each.value
  destination_cidr_block    = module.vpc.vpc_cidr_block
  vpc_peering_connection_id = aws_vpc_peering_connection.pg.id
}
