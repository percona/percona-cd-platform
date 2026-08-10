# Owner: mongodb
# psmdb.cd.percona.com -- Terraform-managed via ./modules/jenkins-master/.
# Migrated from CloudFormation stack jenkins-psmdb
# (Percona-Lab/jenkins-pipelines/IaC/psmdb.cd).
#
# Same cutover as ps3/ps80/ps57/pxb/pxc: off the AWS SpotFleet onto a single
# on-demand instance (purchasing_option), EKS-fronted (TLS terminates at the
# jenkins-masters ALB, reached over cross-region VPC peering on :8080),
# declarative init.groovy.d via the module S3 bucket, and the Graviton ARM
# EC2 Fleet fallback (pre-provisioned while psmdb was still CFN; repointed
# below to the module VPC outputs).
#
# psmdb-specific deltas: fresh VPC reusing the same 10.188.0.0/22 (the CIDR
# is fleet-unique, freed when the CFN stack deletes); legacy JSlave naming
# (the worker instance profile is jenkins-psmdb-slave, referenced by name in
# cloud.groovy); a third subnet in AZ a (the CFN VPC carried a/b/c;
# cloud.groovy's netMap maps all three, the classic EC2 cloud instantiates
# only AZ b, and the ARM fleet spreads across every subnet). The retained 300 GiB
# gp2 data volume is imported in us-west-2b (az_index 1, the module default;
# EBS is AZ-bound so the on-demand instance lands there too).
module "psmdb" {
  source    = "./modules/jenkins-master"
  providers = { aws = aws.us-west-2 }

  hostname                = "psmdb.cd.percona.com"
  short_name              = "jenkins-psmdb"
  team                    = "mongodb"
  vpc_cidr                = "10.188.0.0/22"
  ami_id                  = nonsensitive(data.aws_ssm_parameter.al2023_minimal_usw2.value) # latest AL2023 minimal (amis.tf)
  master_profile          = "eks_observability"
  jenkins_package_version = "2.541.3"
  # psmdb workers have no S3 build cache: no psmdb build cache bucket is
  # wired through this module, so null drops the dead worker S3 IAM grant.
  cache_bucket_name = null

  ssh_allowed_cidrs = local.master_ssh_allowed_cidrs

  # Retained CFN data volume vol-090299a14ad3da940 is 300 GiB gp2 in
  # us-west-2b. ebs_type must be gp2 (not the module default gp3) so the
  # tofu import is a zero-diff adopt; encrypted/iops are ignore_changes in
  # the module.
  ebs_size = 300
  ebs_type = "gp2"

  worker_role_legacy_naming = true
  extra_subnet_a            = true

  # The CFN-era live worker role carried CloudWatchAgentServerPolicy
  # out-of-band; the cutover recreated the role template-shaped and dropped
  # it. Restore the live shape.
  extra_worker_managed_policies = [
    "arn:aws:iam::aws:policy/CloudWatchAgentServerPolicy",
  ]

  # On-demand to end the spot reclamations. c7i-flex.2xlarge (8 vCPU /
  # 16 GB) matches the live master's type and clears its 8 GB JVM heap.
  purchasing_option       = "on-demand"
  on_demand_instance_type = "c7i-flex.2xlarge"

  # 8 GB heap with G1GC tuning for the 16 GiB instance. Masters on 8 GiB
  # boxes use the module default.
  jvm_memory_opts = "-Xms6g -Xmx8g -Xss2m -XX:+UseG1GC -XX:+AlwaysPreTouch -XX:+UseStringDeduplication -XX:MaxGCPauseMillis=200"

  # Distinct LT name so the module's launch template can coexist with the
  # CFN-created master template during the cutover (no name collision,
  # order-independent of delete-stack). The instance references the LT by ID.
  launch_template_name = "PSMDBMasterTemplateTF"

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
      json = data.aws_iam_policy_document.psmdb_alloy_bearer_read.json
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
  # (jenkins-psmdb-init-config). Content moved byte-identically off the live
  # master's EBS copy; cloud.groovy's netMap subnet IDs are patched to the
  # module-created subnets right after the first apply (the IDs do not exist
  # before it). `jenkins iac deploy` stays the no-restart hot-reload path.
  init_groovy_files = {
    for f in fileset("${path.module}/../resources/jenkins-masters/psmdb/init.groovy.d", "*.groovy") :
    f => file("${path.module}/../resources/jenkins-masters/psmdb/init.groovy.d/${f}")
  }
}


# ARM Graviton spot fleet for the ec2-fleet plugin -- the docker-aarch64
# fallback. Pre-provisioned Fleet-only while psmdb was CFN-managed; now
# consumes the TF master's VPC/subnets/role outputs. The worker instance
# profile keeps the legacy jenkins-psmdb-slave name.
module "psmdb_arm_fleet" {
  source    = "./modules/jenkins-arm-fleet"
  providers = { aws = aws.us-west-2 }

  short_name                   = "jenkins-psmdb"
  team                         = "mongodb"
  vpc_id                       = module.psmdb.vpc_id
  subnet_ids                   = module.psmdb.subnet_ids
  worker_instance_profile_name = module.psmdb.worker_instance_profile_name
  master_role_name             = module.psmdb.master_iam_role_name
  key_name                     = "percona-jenkins"
  instance_types               = ["m8g.2xlarge", "m7g.2xlarge", "m6g.2xlarge", "m7gd.2xlarge", "m6gd.2xlarge", "r8g.2xlarge", "r7g.2xlarge", "r6g.2xlarge"]
  max_size                     = 16
  tickets                      = "PS-11179"
}

# Rendered in the root so the ARN uses this account's caller-identity, not
# the us-west-2 alias's. Secret lives in us-east-1 with alloy-gateway.
data "aws_iam_policy_document" "psmdb_alloy_bearer_read" {
  statement {
    sid       = "AlloyGatewayBearerRead"
    effect    = "Allow"
    actions   = ["secretsmanager:GetSecretValue"]
    resources = ["arn:aws:secretsmanager:us-east-1:${data.aws_caller_identity.current.account_id}:secret:percona-ci-platform/alloy-gateway/bearer-*"]
  }
}

# Cross-region VPC peering EKS (us-east-1) <-> psmdb (us-west-2), so the
# in-cluster jenkins-ingress nginx can reach the master's private IP on
# :8080 (TLS offloaded at the jenkins-masters ALB). CIDRs are distinct: EKS
# 10.220.0.0/16, psmdb 10.188.0.0/22.

resource "aws_vpc_peering_connection" "psmdb" {
  vpc_id      = module.vpc.vpc_id
  peer_vpc_id = module.psmdb.vpc_id
  peer_region = "us-west-2"
  auto_accept = false

  tags = {
    Name = "${local.cluster_name}-to-jenkins-psmdb"
  }
}

resource "aws_vpc_peering_connection_accepter" "psmdb" {
  provider                  = aws.us-west-2
  vpc_peering_connection_id = aws_vpc_peering_connection.psmdb.id
  auto_accept               = true

  tags = {
    Name = "${local.cluster_name}-to-jenkins-psmdb"
  }
}

resource "aws_route" "eks_private_to_psmdb" {
  for_each                  = toset(module.vpc.private_route_table_ids)
  route_table_id            = each.value
  destination_cidr_block    = module.psmdb.vpc_cidr
  vpc_peering_connection_id = aws_vpc_peering_connection.psmdb.id
}

# Statically-keyed map (not toset over the IDs): the psmdb VPC is created in
# the same apply, so the route-table IDs are unknown at plan time and a
# value-derived set fails the plan. The module exposes exactly one private
# route table.
resource "aws_route" "psmdb_to_eks" {
  provider                  = aws.us-west-2
  for_each                  = { main = one(module.psmdb.private_route_table_ids) }
  route_table_id            = each.value
  destination_cidr_block    = module.vpc.vpc_cidr_block
  vpc_peering_connection_id = aws_vpc_peering_connection.psmdb.id
}
