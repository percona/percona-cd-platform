# Owner: pmm
# pmm.cd.percona.com -- Terraform-managed via ./modules/jenkins-master/.
# Migrated from CloudFormation stack jenkins-pmm-amzn2
# (Percona-Lab/jenkins-pipelines/IaC/pmm.cd).
#
# Same cutover as ps3/ps80/ps57/pxb/pxc/psmdb/rel/cloud: off the AWS SpotFleet
# onto a single on-demand instance (purchasing_option), EKS-fronted (TLS
# terminates at the jenkins-masters ALB, reached over cross-region VPC peering
# on :8080), declarative init.groovy.d via the module S3 bucket, and the
# Graviton ARM EC2 Fleet fallback (pre-provisioned while pmm was still CFN;
# repointed below to the module VPC outputs).
#
# pmm-specific deltas: the legacy `jenkins-pmm-amzn2` short_name is preserved
# for the VPC tag, IAM names, billing tag, and init-config bucket (the
# endpoint reconciler and worker configs key on it); the ASG keeps the
# fleet-uniform `jenkins-pmm-arm-graviton` name via its own short_name. The
# fresh VPC reuses 10.166.0.0/22 (fleet-unique) and additionally peers with
# the pmm-staging VPC (10.178.0.0/22, same region); that peering is recreated
# at cutover because peering connections die with their VPC. The retained
# 200 GiB gp3 data volume is imported in us-east-2b (az_index 1, the module
# default).
module "pmm" {
  source    = "./modules/jenkins-master"
  providers = { aws = aws.us-east-2 }

  hostname                = "pmm.cd.percona.com"
  short_name              = "jenkins-pmm-amzn2"
  team                    = "pmm"
  vpc_cidr                = "10.166.0.0/22"
  ami_id                  = nonsensitive(data.aws_ssm_parameter.al2023_minimal_use2.value) # latest AL2023 minimal (amis.tf)
  master_profile          = "eks_observability"
  jenkins_package_version = "2.541.3"
  cache_bucket_name       = "pmm-build-cache"

  # Retained CFN data volume vol-00d7246f9eeb1fb72 is 200 GiB gp3 in
  # us-east-2b (already the module default type, so the tofu import is a
  # zero-diff adopt).
  ebs_size = 200

  # On-demand to end the spot reclamations. c7i-flex.xlarge (4 vCPU / 8 GB)
  # clears the 4 GB JVM heap; the prior g4ad.xlarge was a spot-pool artifact,
  # not a GPU requirement.
  purchasing_option       = "on-demand"
  on_demand_instance_type = "c7i-flex.xlarge"

  # Distinct LT name so the module's launch template can coexist with the
  # CFN-created master template during the cutover (no name collision,
  # order-independent of delete-stack). The instance references the LT by ID.
  launch_template_name = "PMMMasterTemplateTF"

  # EKS-fronted for HTTP: DNS is external-dns -> ALB -> private IP over
  # peering, admin via SSM (`just ssh`). EIP-less like ps80/ps57/pxb: the
  # subnet auto-assigns a dynamic public IPv4 for outbound, discovered live
  # via `just ssh` when direct ssh is needed.
  create_eip            = false
  create_route53_record = false

  extra_master_managed_policies = [
    "arn:aws:iam::aws:policy/CloudWatchAgentServerPolicy",
  ]

  extra_master_inline_policies = [
    {
      name = "AlloyGatewayBearerRead"
      json = data.aws_iam_policy_document.pmm_alloy_bearer_read.json
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

  # The 6-CIDR fleet baseline (module default) plus the pmm QA engineer's
  # address. This is the "pmm differs" delta the module variable description
  # references; codified so an apply does not strip the live rule.
  ssh_allowed_cidrs = [
    "46.149.86.84/32",
    "54.214.47.252/32",
    "54.214.47.254/32",
    "154.192.11.141/32",
    "176.37.55.60/32",
    "188.163.20.103/32",
    "213.159.239.48/32",
  ]

  # Declarative init.groovy.d delivered via the module-created S3 bucket
  # (jenkins-pmm-amzn2-init-config). Content moved byte-identically off the
  # live master's EBS copy; cloud.groovy's netMap subnet IDs are patched to
  # the module-created subnets right after the first apply (the IDs do not
  # exist before it). `jenkins iac deploy` stays the no-restart hot-reload
  # path.
  init_groovy_files = {
    for f in fileset("${path.module}/../resources/jenkins-masters/pmm/init.groovy.d", "*.groovy") :
    f => file("${path.module}/../resources/jenkins-masters/pmm/init.groovy.d/${f}")
  }
}


# ARM Graviton spot fleet for the ec2-fleet plugin -- the docker-aarch64
# fallback. Pre-provisioned Fleet-only while pmm was CFN-managed; now
# consumes the TF master's VPC/subnets/role outputs. The ASG keeps the
# fleet-uniform `jenkins-pmm-arm-graviton` name while the master module keeps
# the legacy amzn2 IAM names.
module "pmm_arm_fleet" {
  source    = "./modules/jenkins-arm-fleet"
  providers = { aws = aws.us-east-2 }

  short_name                   = "jenkins-pmm"
  team                         = "pmm"
  vpc_id                       = module.pmm.vpc_id
  subnet_ids                   = module.pmm.subnet_ids
  worker_instance_profile_name = module.pmm.worker_instance_profile_name
  master_role_name             = module.pmm.master_iam_role_name
  key_name                     = "percona-jenkins"
  instance_types               = ["m8g.2xlarge", "m7g.2xlarge", "m6g.2xlarge", "m7gd.2xlarge", "m6gd.2xlarge", "r8g.2xlarge", "r7g.2xlarge", "r6g.2xlarge"]
  max_size                     = 16
  tickets                      = "PS-11179"
}

# Rendered in the root so the ARN uses this account's caller-identity, not
# the us-east-2 alias's. Secret lives in us-east-1 with alloy-gateway.
data "aws_iam_policy_document" "pmm_alloy_bearer_read" {
  statement {
    sid       = "AlloyGatewayBearerRead"
    effect    = "Allow"
    actions   = ["secretsmanager:GetSecretValue"]
    resources = ["arn:aws:secretsmanager:us-east-1:${data.aws_caller_identity.current.account_id}:secret:percona-ci-platform/alloy-gateway/bearer-*"]
  }
}

# Cross-region VPC peering EKS (us-east-1) <-> pmm (us-east-2), so the
# in-cluster jenkins-ingress nginx can reach the master's private IP on
# :8080 (TLS offloaded at the jenkins-masters ALB). CIDRs are distinct: EKS
# 10.220.0.0/16, pmm 10.166.0.0/22, pmm-staging 10.178.0.0/22 (separate
# same-region peering, recreated at cutover outside this file).

resource "aws_vpc_peering_connection" "pmm" {
  vpc_id      = module.vpc.vpc_id
  peer_vpc_id = module.pmm.vpc_id
  peer_region = "us-east-2"
  auto_accept = false

  tags = {
    Name = "${local.cluster_name}-to-jenkins-pmm"
  }
}

resource "aws_vpc_peering_connection_accepter" "pmm" {
  provider                  = aws.us-east-2
  vpc_peering_connection_id = aws_vpc_peering_connection.pmm.id
  auto_accept               = true

  tags = {
    Name = "${local.cluster_name}-to-jenkins-pmm"
  }
}

resource "aws_route" "eks_private_to_pmm" {
  for_each                  = toset(module.vpc.private_route_table_ids)
  route_table_id            = each.value
  destination_cidr_block    = module.pmm.vpc_cidr
  vpc_peering_connection_id = aws_vpc_peering_connection.pmm.id
}

# Statically-keyed map (not toset over the IDs): the pmm VPC is created in
# the same apply, so the route-table IDs are unknown at plan time and a
# value-derived set fails the plan. The module exposes exactly one private
# route table.
resource "aws_route" "pmm_to_eks" {
  provider                  = aws.us-east-2
  for_each                  = { main = one(module.pmm.private_route_table_ids) }
  route_table_id            = each.value
  destination_cidr_block    = module.vpc.vpc_cidr_block
  vpc_peering_connection_id = aws_vpc_peering_connection.pmm.id
}
