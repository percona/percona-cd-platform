# ps57.cd.percona.com -- Terraform-managed via ./modules/jenkins-master/.
# Migrated from CloudFormation stack jenkins-ps57
# (Percona-Lab/jenkins-pipelines/IaC/ps57.cd), PS-11206.
#
# The same cutover applied to ps80: off the AWS SpotFleet onto a single
# on-demand instance (purchasing_option), EKS-fronted (TLS terminates at the
# jenkins-masters ALB, reached over cross-region VPC peering on :8080),
# declarative init.groovy.d via the module S3 bucket, MAX_SURVIVABILITY +
# Hetzner worker rehydration, and the Graviton ARM EC2 Fleet fallback.
#
# ps57-specific deltas vs ps80: region eu-central-1 (ps80 us-west-2); a fresh
# VPC at 10.157.0.0/22 because the live CFN VPC (10.177.0.0/22) collides with
# pxc + cloud and cannot peer to the EKS hub; a non-deprecated AL2023 AMI; a
# smaller m7i-flex.large (2 vCPU / 8 GB, the floor for the master's -Xmx4096m
# heap); and the retained 100 GiB gp2 data volume imported in eu-central-1b.
module "ps57" {
  source    = "./modules/jenkins-master"
  providers = { aws = aws.eu-central-1 }

  hostname                = "ps57.cd.percona.com"
  short_name              = "jenkins-ps57"
  vpc_cidr                = "10.157.0.0/22"
  ami_id                  = nonsensitive(data.aws_ssm_parameter.al2023_minimal_euc1.value) # latest AL2023 minimal (amis.tf)
  master_profile          = "eks_observability"
  jenkins_package_version = "2.541.3"
  cache_bucket_name       = "ps-build-cache"

  # Retained CFN data volume vol-07070c2c983c2cc5f is 100 GiB gp2 in
  # eu-central-1b. ebs_type must be gp2 (not the module default gp3) and
  # az_index 1 (eu-central-1b) so the tofu import is a zero-diff adopt and the
  # on-demand instance lands in the volume's AZ (EBS is AZ-bound).
  ebs_size = 100
  ebs_type = "gp2"
  az_index = 1

  # On-demand to end the spot reclamations (ps80 rationale). m7i-flex.large is
  # 2 vCPU / 8 GB: matches the prior m5a.large footprint and clears the master
  # JVM (-Xms3072m -Xmx4096m); a 4 GB box (c7i-flex.large) would OOM.
  purchasing_option       = "on-demand"
  on_demand_instance_type = "m7i-flex.large"

  # Distinct LT name so the module's launch template can coexist with the
  # CFN-created PS57MasterTemplate during the cutover (no name collision,
  # order-independent of delete-stack). The instance references the LT by ID.
  launch_template_name = "PS57MasterTemplateTF"

  # EKS-fronted like ps80: DNS is external-dns -> ALB -> private IP over
  # peering, admin is paws/SSM, EIP-less (outbound rides the auto-assigned
  # public IP from the public subnet's MapPublicIpOnLaunch + 0.0.0.0/0 -> IGW).
  create_eip            = false
  create_route53_record = false

  extra_master_managed_policies = [
    "arn:aws:iam::aws:policy/CloudWatchAgentServerPolicy",
  ]

  extra_master_inline_policies = [
    {
      name = "AlloyGatewayBearerRead"
      json = data.aws_iam_policy_document.ps57_alloy_bearer_read.json
    },
  ]

  # :8080 from the EKS VPC over cross-region peering for the jenkins-ingress
  # nginx (TLS offloaded at the ALB). Matches the SG ingress so the import
  # diff is zero.
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

  # PS-11173/PS-11179: declarative init.groovy.d delivered via the
  # module-created S3 bucket (jenkins-ps57-init-config). Each file under
  # resources/jenkins-masters/ps57/init.groovy.d/ is uploaded by Terraform and
  # pulled at boot, self-healing a fresh-volume rebuild. cloud.groovy + matrix
  # come from ps57's own jenkins-pipelines files (eu-central-1 subnets/AMIs +
  # PS auth groups), htz.cloud.groovy from the hetzner branch, ec2FleetCloud
  # from the PS-11179 codify. `jenkins iac deploy` stays the no-restart
  # hot-reload path between boots.
  init_groovy_files = {
    for f in fileset("${path.module}/../resources/jenkins-masters/ps57/init.groovy.d", "*.groovy") :
    f => file("${path.module}/../resources/jenkins-masters/ps57/init.groovy.d/${f}")
  }
}

# PS-11179: ARM Graviton spot fleet for the ec2-fleet plugin -- the
# docker-aarch64 fallback when Hetzner CAX capacity is unavailable.
# capacity-optimized across m8g/m7g/m6g.2xlarge. ec2FleetCloud.groovy points
# ps57's EC2FleetCloud at this ASG (jenkins-ps57-arm-graviton) on the
# docker-32gb-aarch64 label. $0 idle (min_size/desired 0); the ec2-fleet
# plugin drives DesiredCapacity. Now consumes the TF master's VPC/subnets/role
# outputs (was the CFN VPC via data sources pre-cutover).
module "ps57_arm_fleet" {
  source    = "./modules/jenkins-arm-fleet"
  providers = { aws = aws.eu-central-1 }

  short_name                   = "jenkins-ps57"
  vpc_id                       = module.ps57.vpc_id
  subnet_ids                   = module.ps57.subnet_ids
  worker_instance_profile_name = module.ps57.worker_instance_profile_name
  master_role_name             = module.ps57.master_iam_role_name
  key_name                     = "percona-jenkins"
  instance_types               = ["m8g.2xlarge", "m7g.2xlarge", "m6g.2xlarge"]
  max_size                     = 16
  tickets                      = "PS-11179"
}

# Rendered in the root so the ARN uses this account's caller-identity, not
# the eu-central-1 alias's. Secret lives in us-east-1 with alloy-gateway.
data "aws_iam_policy_document" "ps57_alloy_bearer_read" {
  statement {
    sid       = "AlloyGatewayBearerRead"
    effect    = "Allow"
    actions   = ["secretsmanager:GetSecretValue"]
    resources = ["arn:aws:secretsmanager:us-east-1:${data.aws_caller_identity.current.account_id}:secret:percona-ci-platform/alloy-gateway/bearer-*"]
  }
}

# Cross-region VPC peering EKS (us-east-1) <-> ps57 (eu-central-1), so the
# in-cluster jenkins-ingress nginx can reach the master's private IP on :8080
# (TLS offloaded at the jenkins-masters ALB). Mirrors the ps80 peering. CIDRs
# are distinct: EKS 10.220.0.0/16, ps57 10.157.0.0/22.

resource "aws_vpc_peering_connection" "ps57" {
  vpc_id      = module.vpc.vpc_id
  peer_vpc_id = module.ps57.vpc_id
  peer_region = "eu-central-1"
  auto_accept = false

  tags = {
    Name = "${local.cluster_name}-to-jenkins-ps57"
  }
}

resource "aws_vpc_peering_connection_accepter" "ps57" {
  provider                  = aws.eu-central-1
  vpc_peering_connection_id = aws_vpc_peering_connection.ps57.id
  auto_accept               = true

  tags = {
    Name = "${local.cluster_name}-to-jenkins-ps57"
  }
}

resource "aws_route" "eks_private_to_ps57" {
  for_each                  = toset(module.vpc.private_route_table_ids)
  route_table_id            = each.value
  destination_cidr_block    = module.ps57.vpc_cidr
  vpc_peering_connection_id = aws_vpc_peering_connection.ps57.id
}

resource "aws_route" "ps57_to_eks" {
  provider                  = aws.eu-central-1
  for_each                  = toset(module.ps57.private_route_table_ids)
  route_table_id            = each.value
  destination_cidr_block    = module.vpc.vpc_cidr_block
  vpc_peering_connection_id = aws_vpc_peering_connection.ps57.id
}
