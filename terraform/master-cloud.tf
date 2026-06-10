# Owner: cloud
# cloud.cd.percona.com -- Terraform-managed via ./modules/jenkins-master/.
# Migrated from CloudFormation stack jenkins-cloud
# (Percona-Lab/jenkins-pipelines/IaC/cloud.cd).
#
# Same cutover as ps3/ps80/ps57/pxb/pxc/psmdb: off the AWS SpotFleet onto a
# single on-demand instance (purchasing_option), EKS-fronted (TLS terminates
# at the jenkins-masters ALB, reached over cross-region VPC peering on
# :8080), declarative init.groovy.d via the module S3 bucket, and the
# Graviton ARM EC2 Fleet fallback (pre-provisioned while cloud was still CFN;
# repointed below to the module VPC outputs).
#
# cloud-specific deltas: fresh VPC reusing the same 10.177.0.0/22 (cloud kept
# this CIDR as the sole occupant after the pxc/ps57 re-CIDRs, so it is
# fleet-unique and can peer); the codified cloud.groovy retires the classic
# docker-32gb-aarch64 template so the Graviton fleet is the label's sole
# provider. The retained 100 GiB gp2 data volume is imported in eu-west-1b
# (az_index 1, the module default; EBS is AZ-bound so the on-demand instance
# lands there too).
module "cloud" {
  source    = "./modules/jenkins-master"
  providers = { aws = aws.eu-west-1 }

  hostname                = "cloud.cd.percona.com"
  short_name              = "jenkins-cloud"
  # Deliberately NOT the bare value "cloud": the cloud team's hourly
  # deleteOrphaned* Lambda suite (eu-west-3) terminates any running instance
  # tagged team=cloud without a delete-cluster-after-hours TTL tag, assuming
  # it is an orphaned OpenShift test cluster. It killed the first TF master
  # within the hour. cloud-cd keeps per-team cost attribution while staying
  # outside that reaper's match.
  team                    = "cloud-cd"
  vpc_cidr                = "10.177.0.0/22"
  ami_id                  = nonsensitive(data.aws_ssm_parameter.al2023_minimal_euw1.value) # latest AL2023 minimal (amis.tf)
  master_profile          = "eks_observability"
  jenkins_package_version = "2.541.3"
  cache_bucket_name       = "cloud-build-cache"

  # Retained CFN data volume vol-077265221bc0180b3 is 100 GiB gp2 in
  # eu-west-1b. ebs_type must be gp2 (not the module default gp3) so the
  # tofu import is a zero-diff adopt; encrypted/iops are ignore_changes in
  # the module.
  ebs_size = 100
  ebs_type = "gp2"

  # On-demand to end the spot reclamations. c7i-flex.xlarge (4 vCPU / 8 GB)
  # matches the prior c5a.xlarge footprint and clears the 4 GB JVM heap.
  purchasing_option       = "on-demand"
  on_demand_instance_type = "c7i-flex.xlarge"

  # Distinct LT name so the module's launch template can coexist with the
  # CFN-created master template during the cutover (no name collision,
  # order-independent of delete-stack). The instance references the LT by ID.
  launch_template_name = "CLOUDMasterTemplateTF"

  # EKS-fronted for HTTP: DNS is external-dns -> ALB -> private IP over
  # peering, admin via paws/SSM. create_eip stays true through the cutover
  # (SSH path once the hostname resolves to the ALB); flipping to the
  # ps80-style EIP-less shape is a follow-up.
  create_eip            = true
  create_route53_record = false

  extra_master_managed_policies = [
    "arn:aws:iam::aws:policy/CloudWatchAgentServerPolicy",
  ]

  extra_master_inline_policies = [
    {
      name = "AlloyGatewayBearerRead"
      json = data.aws_iam_policy_document.cloud_alloy_bearer_read.json
    },
  ]

  # :8080 from the EKS VPC over cross-region peering for the jenkins-ingress
  # nginx (TLS offloaded at the ALB).
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

  # Declarative init.groovy.d delivered via the module-created S3 bucket
  # (jenkins-cloud-init-config). Content moved byte-identically off the live
  # master's EBS copy; cloud.groovy's netMap subnet IDs are patched to the
  # module-created subnets right after the first apply (the IDs do not exist
  # before it). `jenkins iac deploy` stays the no-restart hot-reload path.
  init_groovy_files = {
    for f in fileset("${path.module}/../resources/jenkins-masters/cloud/init.groovy.d", "*.groovy") :
    f => file("${path.module}/../resources/jenkins-masters/cloud/init.groovy.d/${f}")
  }
}

# One-shot adopt of the retained CFN data volume (JENKINS_HOME). Remove this
# block after the cutover apply lands.
import {
  to = module.cloud.aws_ebs_volume.data
  id = "vol-077265221bc0180b3"
}

# ARM Graviton spot fleet for the ec2-fleet plugin -- the docker-aarch64
# fallback. Pre-provisioned
# Fleet-only while cloud was CFN-managed; now consumes the TF master's
# VPC/subnets/role outputs.
module "cloud_arm_fleet" {
  source    = "./modules/jenkins-arm-fleet"
  providers = { aws = aws.eu-west-1 }

  short_name                   = "jenkins-cloud"
  # cloud-cd, not "cloud": see the master module's team comment (the
  # OpenShift orphan reaper matches team=cloud instances, including Graviton
  # fleet workers mid-build).
  team                         = "cloud-cd"
  vpc_id                       = module.cloud.vpc_id
  subnet_ids                   = module.cloud.subnet_ids
  worker_instance_profile_name = module.cloud.worker_instance_profile_name
  master_role_name             = module.cloud.master_iam_role_name
  key_name                     = "percona-jenkins"
  instance_types               = ["m8g.2xlarge", "m7g.2xlarge", "m6g.2xlarge", "m7gd.2xlarge", "m6gd.2xlarge", "r8g.2xlarge", "r7g.2xlarge", "r6g.2xlarge"]
  max_size                     = 16
  tickets                      = "PS-11179"
}

# Rendered in the root so the ARN uses this account's caller-identity, not
# the eu-west-1 alias's. Secret lives in us-east-1 with alloy-gateway.
data "aws_iam_policy_document" "cloud_alloy_bearer_read" {
  statement {
    sid       = "AlloyGatewayBearerRead"
    effect    = "Allow"
    actions   = ["secretsmanager:GetSecretValue"]
    resources = ["arn:aws:secretsmanager:us-east-1:${data.aws_caller_identity.current.account_id}:secret:percona-ci-platform/alloy-gateway/bearer-*"]
  }
}

# Cross-region VPC peering EKS (us-east-1) <-> cloud (eu-west-1), so the
# in-cluster jenkins-ingress nginx can reach the master's private IP on
# :8080 (TLS offloaded at the jenkins-masters ALB). CIDRs are distinct: EKS
# 10.220.0.0/16, cloud 10.177.0.0/22, ps3 10.181.0.0/22 + rel 10.199.0.0/22 (same region).

resource "aws_vpc_peering_connection" "cloud" {
  vpc_id      = module.vpc.vpc_id
  peer_vpc_id = module.cloud.vpc_id
  peer_region = "eu-west-1"
  auto_accept = false

  tags = {
    Name = "${local.cluster_name}-to-jenkins-cloud"
  }
}

resource "aws_vpc_peering_connection_accepter" "cloud" {
  provider                  = aws.eu-west-1
  vpc_peering_connection_id = aws_vpc_peering_connection.cloud.id
  auto_accept               = true

  tags = {
    Name = "${local.cluster_name}-to-jenkins-cloud"
  }
}

resource "aws_route" "eks_private_to_cloud" {
  for_each                  = toset(module.vpc.private_route_table_ids)
  route_table_id            = each.value
  destination_cidr_block    = module.cloud.vpc_cidr
  vpc_peering_connection_id = aws_vpc_peering_connection.cloud.id
}

# Statically-keyed map (not toset over the IDs): the cloud VPC is created in
# the same apply, so the route-table IDs are unknown at plan time and a
# value-derived set fails the plan. The module exposes exactly one private
# route table.
resource "aws_route" "cloud_to_eks" {
  provider                  = aws.eu-west-1
  for_each                  = { main = one(module.cloud.private_route_table_ids) }
  route_table_id            = each.value
  destination_cidr_block    = module.vpc.vpc_cidr_block
  vpc_peering_connection_id = aws_vpc_peering_connection.cloud.id
}
