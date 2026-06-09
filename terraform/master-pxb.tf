# Owner: xtrabackup
# pxb.cd.percona.com -- Terraform-managed via ./modules/jenkins-master/.
# Migrated from the legacy standalone Terraform config
# (Percona-Lab/jenkins-pipelines/IaC/pxb.cd/terraform, S3 backend
# terraform-state-storage-pxb). Unlike ps3/ps80/ps57 (which were CloudFormation),
# pxb was already Terraform, so this cutover is an import + state-rm move, not a
# delete-stack: the live VPC, subnets, SGs, IAM roles, EBS data volume, SQS queue
# are imported into the module addresses below, then forgotten from the old state.
#
# Same shape as the other migrated masters: off the SpotFleet onto a single
# on-demand instance (purchasing_option), EKS-fronted (TLS terminates at the
# jenkins-masters ALB, reached over cross-region VPC peering on :8080),
# declarative init.groovy.d via the module S3 bucket, MAX_SURVIVABILITY + Hetzner
# rehydration, and the Graviton ARM EC2 Fleet fallback.
#
# pxb-specific deltas:
#   - KEEP-VPC: pxb's 10.179.0.0/22 is unique (no collision with the EKS hub
#     10.220.0.0/16 or any peered master), so the live VPC/subnets are imported,
#     not recreated (the ps3 precedent, not the ps57 re-CIDR).
#   - on-demand m7i-flex.large (2 vCPU / 8 GB): matches the prior m4.large and
#     clears the master JVM (-Xms3072m); CPU is trivially idle.
#   - retained 250 GiB gp2 data volume (vol-08cacafbce3ce7fdd) in us-west-2b, so
#     ebs_type=gp2 (not the module default gp3) and az_index=1 (us-west-2b) for a
#     zero-diff import and to land the instance in the volume's AZ (EBS is AZ-bound).
#   - latest AL2023 minimal sourced from the shared SSM parameter (amis.tf).
module "pxb" {
  source    = "./modules/jenkins-master"
  providers = { aws = aws.us-west-2 }

  hostname                = "pxb.cd.percona.com"
  short_name              = "jenkins-pxb"
  vpc_cidr                = "10.179.0.0/22"
  ami_id                  = nonsensitive(data.aws_ssm_parameter.al2023_minimal_usw2.value)
  master_profile          = "eks_observability"
  jenkins_package_version = "2.541.3"
  # pxb workers do not use an S3 build cache (null disables the worker S3 IAM
  # policy), matching the legacy pxb worker role.
  cache_bucket_name = null

  # Retained CFN-era data volume vol-08cacafbce3ce7fdd is 250 GiB gp2 in
  # us-west-2b. ebs_type must be gp2 (not the module default gp3) and az_index 1
  # (us-west-2b) so the tofu import is a zero-diff adopt and the on-demand
  # instance lands in the volume's AZ.
  ebs_size = 250
  ebs_type = "gp2"
  az_index = 1

  # On-demand to end the spot reclamations (ps80/ps57 rationale). m7i-flex.large
  # is 2 vCPU / 8 GB: matches the prior m4.large footprint and clears the master
  # JVM (-Xms3072m); a 4 GB box (c7i-flex.large) would OOM.
  purchasing_option       = "on-demand"
  on_demand_instance_type = "m7i-flex.large"

  # Distinct LT name so the module's launch template can coexist with the
  # legacy-created template during the cutover (no name collision). The instance
  # references the LT by ID.
  launch_template_name = "PXBMasterTemplateTF"

  # EKS-fronted like ps80/ps57: DNS is external-dns -> ALB -> private IP over
  # peering, admin is paws/SSM, EIP-less (outbound rides the auto-assigned public
  # IP from the public subnet's MapPublicIpOnLaunch + 0.0.0.0/0 -> IGW). The old
  # A -> EIP record is destroyed via the legacy state so external-dns publishes
  # pxb.cd -> ALB.
  create_eip            = false
  create_route53_record = false

  extra_master_managed_policies = [
    "arn:aws:iam::aws:policy/CloudWatchAgentServerPolicy",
  ]

  extra_master_inline_policies = [
    {
      name = "AlloyGatewayBearerRead"
      json = data.aws_iam_policy_document.pxb_alloy_bearer_read.json
    },
  ]

  # :8080 from the EKS VPC over cross-region peering for the jenkins-ingress
  # nginx (TLS offloaded at the ALB). Matches the live SG ingress so the import
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

  # Declarative init.groovy.d delivered via the module-created
  # S3 bucket (jenkins-pxb-init-config). Each file under
  # resources/jenkins-masters/pxb/init.groovy.d/ is uploaded by Terraform and
  # pulled at boot, self-healing a fresh-volume rebuild. cloud.groovy + matrix
  # come from pxb's own jenkins-pipelines files (us-west-2 subnets/AMIs + PXB auth
  # groups), htz.cloud.groovy from the hetzner branch, ec2FleetCloud from the
  # fleet codify. The classic aarch64 templates are i4g.2xlarge, NOT m8g
  # (ported from the classic pxb-origin templates). `jenkins iac deploy` stays the no-restart
  # hot-reload path between boots.
  init_groovy_files = {
    for f in fileset("${path.module}/../resources/jenkins-masters/pxb/init.groovy.d", "*.groovy") :
    f => file("${path.module}/../resources/jenkins-masters/pxb/init.groovy.d/${f}")
  }
}

# ARM Graviton spot fleet for the ec2-fleet plugin -- the
# docker-aarch64 fallback when Hetzner CAX capacity is unavailable.
# capacity-optimized across m8g/m7g/m6g.2xlarge. ec2FleetCloud.groovy points
# pxb's EC2FleetCloud at this ASG (jenkins-pxb-arm-graviton) on the
# docker-32gb-aarch64 label. $0 idle (min_size/desired 0); the ec2-fleet plugin
# drives DesiredCapacity.
module "pxb_arm_fleet" {
  source    = "./modules/jenkins-arm-fleet"
  providers = { aws = aws.us-west-2 }

  short_name                   = "jenkins-pxb"
  vpc_id                       = module.pxb.vpc_id
  subnet_ids                   = module.pxb.subnet_ids
  worker_instance_profile_name = module.pxb.worker_instance_profile_name
  master_role_name             = module.pxb.master_iam_role_name
  key_name                     = "percona-jenkins"
  instance_types               = ["m8g.2xlarge", "m7g.2xlarge", "m6g.2xlarge"]
  max_size                     = 16
  tickets                      = "PS-11179"
}

# Rendered in the root so the ARN uses this account's caller-identity, not the
# us-west-2 alias's. Secret lives in us-east-1 with the rest of alloy-gateway.
data "aws_iam_policy_document" "pxb_alloy_bearer_read" {
  statement {
    sid       = "AlloyGatewayBearerRead"
    effect    = "Allow"
    actions   = ["secretsmanager:GetSecretValue"]
    resources = ["arn:aws:secretsmanager:us-east-1:${data.aws_caller_identity.current.account_id}:secret:percona-ci-platform/alloy-gateway/bearer-*"]
  }
}

# Cross-region VPC peering EKS (us-east-1) <-> pxb (us-west-2), so the in-cluster
# jenkins-ingress nginx can reach the master's private IP on :8080 (TLS offloaded
# at the jenkins-masters ALB). Mirrors the ps80 peering. CIDRs are distinct:
# EKS 10.220.0.0/16, pxb 10.179.0.0/22.

resource "aws_vpc_peering_connection" "pxb" {
  vpc_id      = module.vpc.vpc_id
  peer_vpc_id = module.pxb.vpc_id
  peer_region = "us-west-2"
  auto_accept = false

  tags = {
    Name = "${local.cluster_name}-to-jenkins-pxb"
  }
}

resource "aws_vpc_peering_connection_accepter" "pxb" {
  provider                  = aws.us-west-2
  vpc_peering_connection_id = aws_vpc_peering_connection.pxb.id
  auto_accept               = true

  tags = {
    Name = "${local.cluster_name}-to-jenkins-pxb"
  }
}

resource "aws_route" "eks_private_to_pxb" {
  for_each                  = toset(module.vpc.private_route_table_ids)
  route_table_id            = each.value
  destination_cidr_block    = module.pxb.vpc_cidr
  vpc_peering_connection_id = aws_vpc_peering_connection.pxb.id
}

resource "aws_route" "pxb_to_eks" {
  provider                  = aws.us-west-2
  for_each                  = toset(module.pxb.private_route_table_ids)
  route_table_id            = each.value
  destination_cidr_block    = module.vpc.vpc_cidr_block
  vpc_peering_connection_id = aws_vpc_peering_connection.pxb.id
}
