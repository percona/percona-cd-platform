# Owner: mysql
# ps80.cd.percona.com -- Terraform-managed via ./modules/jenkins-master/.
# Migrated from CloudFormation stack jenkins-ps80
# (Percona-Lab/jenkins-pipelines/IaC/ps80.cd).
#
# Switched from SpotFleet to a single on-demand instance via the module's
# purchasing_option toggle. Three spot reclamations in 4 days (2026-05-25/26)
# killed MTR MatrixBuild parents (#38/#39/#40) and arm64 child builds;
# on-demand at right-sized c7i-flex.large is +$17/mo over the prior spot
# blend and pays back on the first avoided reclamation.
#
# The TF module runs Jenkins on plain :8080 with no master-side TLS, so this
# cut is EKS-fronted like ps3: TLS terminates at the jenkins-masters
# ALB, reached over cross-region VPC peering on :8080. It also folds in the
# resilience workstreams sequenced to land here: init.groovy.d auto-loading,
# MAX_SURVIVABILITY + Hetzner rehydration, and the Graviton ARM EC2 Fleet
# fallback. EIP/Route53 stayed TRUE through the cutover and flipped to false
# at the Window-1 DNS step.

module "ps80" {
  source    = "./modules/jenkins-master"
  providers = { aws = aws.us-west-2 }

  hostname                = "ps80.cd.percona.com"
  short_name              = "jenkins-ps80"
  team                    = "mysql"
  vpc_cidr                = "10.155.0.0/22"
  ami_id                  = nonsensitive(data.aws_ssm_parameter.al2023_minimal_usw2.value) # latest AL2023 minimal (amis.tf)
  master_profile          = "eks_observability"
  ssh_allowed_cidrs       = local.master_ssh_allowed_cidrs
  jenkins_package_version = "2.541.3"
  cache_bucket_name       = "ps-build-cache"
  ebs_size                = 300 # grown live from 200; EBS cannot shrink, keep >= live size

  purchasing_option = "on-demand"
  # 4 vCPU / 8 GB: the master JVM is -Xms3072m -Xmx4096m, so a 4 GB box
  # (c7i-flex.large) would OOM. xlarge keeps parity with the prior c5d.xlarge
  # while CPU stays trivially idle (p95 < 2%). On-demand ~$0.17/hr.
  on_demand_instance_type = "c7i-flex.xlarge"

  # Distinct LT name so the module's launch template can coexist with the
  # CFN-created `PS80MasterTemplate` during the cutover (no name collision,
  # order-independent of delete-stack). The instance references the LT by ID.
  launch_template_name = "PS80MasterTemplateTF"

  # EKS-fronted, so no public ingress need: DNS is external-dns -> ALB ->
  # private IP over peering, admin is paws/SSM. The master is EIP-less like
  # ps3: outbound (Hetzner + AWS APIs) rides the auto-assigned public IP from
  # the public subnet's MapPublicIpOnLaunch=true + 0.0.0.0/0 -> IGW. Releasing
  # the EIP needs the instance replaced so it picks up an auto-assigned IP.
  create_eip      = false
  master_key_name = "percona-jenkins"

  extra_master_managed_policies = [
    "arn:aws:iam::aws:policy/CloudWatchAgentServerPolicy",
  ]

  extra_master_inline_policies = [
    {
      name = "AlloyGatewayBearerRead"
      json = data.aws_iam_policy_document.ps80_alloy_bearer_read.json
    },
  ]

  # :8080 from the EKS VPC over cross-region peering for the jenkins-ingress
  # nginx (TLS offloaded at the ALB). Matches the live SG ingress opened in
  # Window 1 so the import diff is zero.
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

  # Declarative init.groovy.d wiring delivered via the
  # module-created S3 bucket (jenkins-ps80-init-config). The repo is now the
  # source of truth: each file under resources/jenkins-masters/ps80/init.groovy.d/
  # is uploaded by Terraform and pulled at boot, self-healing a fresh-volume
  # rebuild. Content was moved byte-identically off Percona-Lab/jenkins-pipelines
  # (PR 4134 tip for cloud/matrix/durability/hetznerArmHealth/ec2FleetCloud, the
  # hetzner branch for htz.cloud.groovy). `jenkins iac deploy` stays the
  # no-restart hot-reload path between boots.
  init_groovy_files = {
    for f in fileset("${path.module}/../resources/jenkins-masters/ps80/init.groovy.d", "*.groovy") :
    f => file("${path.module}/../resources/jenkins-masters/ps80/init.groovy.d/${f}")
  }
  init_groovy_sync_schedule = "rate(30 minutes)"
}

# ARM Graviton spot fleet for the ec2-fleet plugin -- the
# docker-aarch64 fallback when Hetzner CAX capacity is unavailable.
# capacity-optimized across m8g/m7g/m6g.2xlarge (SPS ~9 vs ~3 single-type).
# ec2FleetCloud.groovy points ps80's EC2FleetCloud at this ASG
# (jenkins-ps80-arm-graviton) on the docker-32gb-aarch64 label. $0 idle
# (min_size/desired 0); the ec2-fleet plugin drives DesiredCapacity.
module "ps80_arm_fleet" {
  source    = "./modules/jenkins-arm-fleet"
  providers = { aws = aws.us-west-2 }

  short_name                   = "jenkins-ps80"
  team                         = "mysql"
  vpc_id                       = module.ps80.vpc_id
  subnet_ids                   = module.ps80.subnet_ids
  worker_instance_profile_name = module.ps80.worker_instance_profile_name
  master_role_name             = module.ps80.master_iam_role_name
  key_name                     = "percona-jenkins"
  instance_types               = ["m8g.2xlarge", "m7g.2xlarge", "m6g.2xlarge", "m7gd.2xlarge", "m6gd.2xlarge", "r8g.2xlarge", "r7g.2xlarge", "r6g.2xlarge"]
  max_size                     = 16
  tickets                      = "PS-11179"
}

# Rendered in the root so the ARN uses this account's caller-identity, not
# the us-west-2 alias's. Secret lives in us-east-1 with the rest of
# alloy-gateway.
data "aws_iam_policy_document" "ps80_alloy_bearer_read" {
  statement {
    sid       = "AlloyGatewayBearerRead"
    effect    = "Allow"
    actions   = ["secretsmanager:GetSecretValue"]
    resources = ["arn:aws:secretsmanager:us-east-1:${data.aws_caller_identity.current.account_id}:secret:percona-ci-platform/alloy-gateway/bearer-*"]
  }
}

# Cross-region VPC peering EKS (us-east-1) <-> ps80 (us-west-2), so the
# in-cluster jenkins-ingress nginx can reach the master's private IP on :8080
# (TLS offloaded at the jenkins-masters ALB). Mirrors the ps3 peering
# (master-ps3.tf). CIDRs are distinct: EKS 10.220.0.0/16,
# ps3 10.181.0.0/22, ps80 10.155.0.0/22.

resource "aws_vpc_peering_connection" "ps80" {
  vpc_id      = module.vpc.vpc_id
  peer_vpc_id = module.ps80.vpc_id
  peer_region = "us-west-2"
  auto_accept = false

  tags = {
    Name = "${local.cluster_name}-to-jenkins-ps80"
  }
}

resource "aws_vpc_peering_connection_accepter" "ps80" {
  provider                  = aws.us-west-2
  vpc_peering_connection_id = aws_vpc_peering_connection.ps80.id
  auto_accept               = true

  tags = {
    Name = "${local.cluster_name}-to-jenkins-ps80"
  }
}

resource "aws_route" "eks_private_to_ps80" {
  for_each                  = toset(module.vpc.private_route_table_ids)
  route_table_id            = each.value
  destination_cidr_block    = module.ps80.vpc_cidr
  vpc_peering_connection_id = aws_vpc_peering_connection.ps80.id
}

resource "aws_route" "ps80_to_eks" {
  provider                  = aws.us-west-2
  for_each                  = toset(module.ps80.private_route_table_ids)
  route_table_id            = each.value
  destination_cidr_block    = module.vpc.vpc_cidr_block
  vpc_peering_connection_id = aws_vpc_peering_connection.ps80.id
}
