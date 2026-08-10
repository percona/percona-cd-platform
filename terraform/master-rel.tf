# Owner: release
# rel.cd.percona.com -- Terraform-managed via ./modules/jenkins-master/.
# Migrated from CloudFormation stack jenkins-rel
# (Percona-Lab/jenkins-pipelines/IaC/rel.cd).
#
# Same cutover as ps3/ps80/ps57/pxb/pxc/psmdb: off the AWS SpotFleet onto a
# single on-demand instance (purchasing_option), EKS-fronted (TLS terminates
# at the jenkins-masters ALB, reached over cross-region VPC peering on
# :8080), declarative init.groovy.d via the module S3 bucket, and the
# Graviton ARM EC2 Fleet fallback (pre-provisioned while rel was still CFN;
# repointed below to the module VPC outputs).
#
# rel-specific deltas: fresh VPC reusing the same 10.199.0.0/22 (fleet-unique
# CIDR, freed when the CFN stack deletes); release builds also consume the
# docker-aarch64 label on the Graviton fleet. The retained 100 GiB gp2 data
# volume is imported in eu-west-1b (az_index 1, the module default; EBS is
# AZ-bound so the on-demand instance lands there too).
module "rel" {
  source    = "./modules/jenkins-master"
  providers = { aws = aws.eu-west-1 }

  hostname                = "rel.cd.percona.com"
  short_name              = "jenkins-rel"
  team                    = "release"
  vpc_cidr                = "10.199.0.0/22"
  ami_id                  = nonsensitive(data.aws_ssm_parameter.al2023_minimal_euw1.value) # latest AL2023 minimal (amis.tf)
  master_profile          = "eks_observability"
  ssh_allowed_cidrs       = local.master_ssh_allowed_cidrs
  jenkins_package_version = "2.541.3"
  # rel workers have no S3 build cache: the CFN-era default named a
  # rel-build-cache bucket that was never created. null drops the dead
  # worker IAM grant (the existing rel-repo-cache bucket is separate and
  # not wired through this module).
  cache_bucket_name = null

  # Retained CFN data volume vol-01b7479079f91c010 is 100 GiB gp2 in
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
  launch_template_name = "RELMasterTemplateTF"

  # EKS-fronted for HTTP: DNS is external-dns -> ALB -> private IP over
  # peering, admin via SSM (`just ssh`). EIP-less like ps80/ps57/pxb: the
  # subnet auto-assigns a dynamic public IPv4 for outbound, discovered live
  # via `just ssh` when direct ssh is needed.
  create_eip      = false
  master_key_name = "percona-jenkins"

  extra_master_managed_policies = [
    "arn:aws:iam::aws:policy/CloudWatchAgentServerPolicy",
  ]

  extra_master_inline_policies = [
    {
      name = "AlloyGatewayBearerRead"
      json = data.aws_iam_policy_document.rel_alloy_bearer_read.json
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
  # (jenkins-rel-init-config). Content moved byte-identically off the live
  # master's EBS copy; cloud.groovy's netMap subnet IDs are patched to the
  # module-created subnets right after the first apply (the IDs do not exist
  # before it). `jenkins iac deploy` stays the no-restart hot-reload path.
  init_groovy_files = {
    for f in fileset("${path.module}/../resources/jenkins-masters/rel/init.groovy.d", "*.groovy") :
    f => file("${path.module}/../resources/jenkins-masters/rel/init.groovy.d/${f}")
  }
}


# ARM Graviton spot fleet for the ec2-fleet plugin -- the docker-aarch64
# fallback (rel also serves release builds on that label). Pre-provisioned
# Fleet-only while rel was CFN-managed; now consumes the TF master's
# VPC/subnets/role outputs.
module "rel_arm_fleet" {
  source    = "./modules/jenkins-arm-fleet"
  providers = { aws = aws.eu-west-1 }

  short_name                   = "jenkins-rel"
  team                         = "release"
  vpc_id                       = module.rel.vpc_id
  subnet_ids                   = module.rel.subnet_ids
  worker_instance_profile_name = module.rel.worker_instance_profile_name
  master_role_name             = module.rel.master_iam_role_name
  key_name                     = "percona-jenkins"
  instance_types               = ["m8g.2xlarge", "m7g.2xlarge", "m6g.2xlarge", "m7gd.2xlarge", "m6gd.2xlarge", "r8g.2xlarge", "r7g.2xlarge", "r6g.2xlarge"]
  max_size                     = 16
  tickets                      = "PS-11179"
}

# Rendered in the root so the ARN uses this account's caller-identity, not
# the eu-west-1 alias's. Secret lives in us-east-1 with alloy-gateway.
data "aws_iam_policy_document" "rel_alloy_bearer_read" {
  statement {
    sid       = "AlloyGatewayBearerRead"
    effect    = "Allow"
    actions   = ["secretsmanager:GetSecretValue"]
    resources = ["arn:aws:secretsmanager:us-east-1:${data.aws_caller_identity.current.account_id}:secret:percona-ci-platform/alloy-gateway/bearer-*"]
  }
}

# Cross-region VPC peering EKS (us-east-1) <-> rel (eu-west-1), so the
# in-cluster jenkins-ingress nginx can reach the master's private IP on
# :8080 (TLS offloaded at the jenkins-masters ALB). CIDRs are distinct: EKS
# 10.220.0.0/16, rel 10.199.0.0/22, ps3 10.181.0.0/22 (same region).

resource "aws_vpc_peering_connection" "rel" {
  vpc_id      = module.vpc.vpc_id
  peer_vpc_id = module.rel.vpc_id
  peer_region = "eu-west-1"
  auto_accept = false

  tags = {
    Name = "${local.cluster_name}-to-jenkins-rel"
  }
}

resource "aws_vpc_peering_connection_accepter" "rel" {
  provider                  = aws.eu-west-1
  vpc_peering_connection_id = aws_vpc_peering_connection.rel.id
  auto_accept               = true

  tags = {
    Name = "${local.cluster_name}-to-jenkins-rel"
  }
}

resource "aws_route" "eks_private_to_rel" {
  for_each                  = toset(module.vpc.private_route_table_ids)
  route_table_id            = each.value
  destination_cidr_block    = module.rel.vpc_cidr
  vpc_peering_connection_id = aws_vpc_peering_connection.rel.id
}

# Statically-keyed map (not toset over the IDs): the rel VPC is created in
# the same apply, so the route-table IDs are unknown at plan time and a
# value-derived set fails the plan. The module exposes exactly one private
# route table.
resource "aws_route" "rel_to_eks" {
  provider                  = aws.eu-west-1
  for_each                  = { main = one(module.rel.private_route_table_ids) }
  route_table_id            = each.value
  destination_cidr_block    = module.vpc.vpc_cidr_block
  vpc_peering_connection_id = aws_vpc_peering_connection.rel.id
}
