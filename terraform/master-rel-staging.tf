# Owner: release
# rel-staging.cd.percona.com -- staging copy of rel for the Jenkins core
# 2.568.3 + Java 21 upgrade. Boots production rel's
# real JENKINS_HOME from a snapshot-restored data volume on the upgraded core.
# resources/jenkins-masters/rel-staging/init.groovy.d/ clears clouds, agents,
# triggers and executors at boot (a-fence.groovy plus same-name stubs), and
# the IAM deny below stops the master role from starting or terminating
# instances even if a script loses a boot race. Production rel stays untouched.
#
# TEMPORARY: delete this file, the fence set and the addon host entries after
# the production rel roll.
#
# Data volume is adopted, not created:
#   1. aws ec2 create-volume --region eu-west-1 --availability-zone eu-west-1b \
#        --snapshot-id <newest completed DLM snapshot of production rel's data volume> \
#        --volume-type gp2 \
#        --tag-specifications 'ResourceType=volume,Tags=[{Key=Name,Value=jenkins-rel-staging},{Key=iit-billing-tag,Value=jenkins-rel-staging},{Key=PerconaKeep,Value=True}]'
#   2. tofu -chdir=terraform import module.rel_staging.aws_ebs_volume.data vol-XXXX (raw-tofu exception)
#   3. just tf-apply.
# The module's mkfs.xfs runs without -f, so the restored filesystem is never
# wiped. jenkins_home_dirname keeps JENKINS_HOME at the snapshot's directory
# name, so the restored tree is used as-is.
module "rel_staging" {
  source    = "./modules/jenkins-master"
  providers = { aws = aws.eu-west-1 }

  hostname             = "rel-staging.cd.percona.com"
  short_name           = "jenkins-rel-staging"
  team                 = "release"
  vpc_cidr             = "10.161.0.0/22"
  ami_id               = data.aws_ami.al2023_minimal_euw1.id # pinned AL2023 minimal (amis.tf)
  master_profile       = "eks_observability"
  ssh_allowed_cidrs    = local.master_ssh_allowed_cidrs
  engineer_roster      = local.master_ssh_engineer_roster
  cache_bucket_name    = null
  launch_template_name = "RelStagingMasterTemplate"

  # The upgrade under test. Production rel keeps its current core until its roll.
  jenkins_package_version = "2.568.3"
  java_package            = "java-21-amazon-corretto-headless"

  # JENKINS_HOME stays at the snapshot's directory name (/mnt/rel.cd.percona.com).
  # The Alloy master label still reports this module's hostname, so staging
  # telemetry never mixes with production rel's series.
  jenkins_home_dirname = "rel.cd.percona.com"

  # Same volume geometry as production rel (100 GiB gp2 in eu-west-1b).
  ebs_size = 100
  ebs_type = "gp2"
  az_index = 1

  purchasing_option       = "on-demand"
  on_demand_instance_type = "c7i-flex.xlarge"

  create_eip      = false
  master_key_name = "percona-jenkins"

  extra_master_managed_policies = [
    "arn:aws:iam::aws:policy/CloudWatchAgentServerPolicy",
  ]

  extra_master_inline_policies = [
    {
      name = "AlloyGatewayBearerRead"
      json = data.aws_iam_policy_document.rel_staging_alloy_bearer_read.json
    },
    # Explicit deny beats the module's StartInstances allow, so a boot-race
    # window where the restored config.xml still lists production clouds
    # cannot run or terminate EC2 instances.
    {
      name = "DenyWorkerProvisioning"
      json = data.aws_iam_policy_document.rel_staging_deny_provisioning.json
    },
  ]

  # :8080 from the EKS VPC over cross-region peering for the jenkins-ingress
  # nginx (TLS offloaded at the ALB).
  extra_http_ingress = [
    { port = 8080, cidr = module.vpc.vpc_cidr_block },
  ]

  # Fence set, NOT production rel's files.
  init_groovy_files = {
    for f in fileset("${path.module}/../resources/jenkins-masters/rel-staging/init.groovy.d", "*.groovy") :
    f => file("${path.module}/../resources/jenkins-masters/rel-staging/init.groovy.d/${f}")
  }
  init_groovy_sync_schedule = "rate(30 minutes)"
}

# No arm-fleet sibling: the staging copy provisions no workers, and the ASG
# jenkins-rel-arm-graviton belongs to production rel.

data "aws_iam_policy_document" "rel_staging_alloy_bearer_read" {
  statement {
    sid       = "AlloyGatewayBearerRead"
    effect    = "Allow"
    actions   = ["secretsmanager:GetSecretValue"]
    resources = ["arn:aws:secretsmanager:us-east-1:${data.aws_caller_identity.current.account_id}:secret:percona-ci-platform/alloy-gateway/bearer-*"]
  }
}

data "aws_iam_policy_document" "rel_staging_deny_provisioning" {
  statement {
    sid    = "DenyWorkerProvisioning"
    effect = "Deny"
    actions = [
      "ec2:RunInstances",
      "ec2:StartInstances",
      "ec2:StopInstances",
      "ec2:TerminateInstances",
      "ec2:RequestSpotInstances",
      "ec2:ModifySpotFleetRequest",
      "ec2:CancelSpotInstanceRequests",
    ]
    resources = ["*"]
  }
}

# Cross-region VPC peering EKS (us-east-1) <-> rel-staging (eu-west-1), so the
# in-cluster jenkins-ingress nginx can reach the master's private IP on :8080.
# CIDRs: EKS 10.220.0.0/16, rel-staging 10.161.0.0/22.

resource "aws_vpc_peering_connection" "rel_staging" {
  vpc_id      = module.vpc.vpc_id
  peer_vpc_id = module.rel_staging.vpc_id
  peer_region = "eu-west-1"
  auto_accept = false

  tags = {
    Name = "${local.cluster_name}-to-jenkins-rel-staging"
  }
}

resource "aws_vpc_peering_connection_accepter" "rel_staging" {
  provider                  = aws.eu-west-1
  vpc_peering_connection_id = aws_vpc_peering_connection.rel_staging.id
  auto_accept               = true

  tags = {
    Name = "${local.cluster_name}-to-jenkins-rel-staging"
  }
}

resource "aws_route" "eks_private_to_rel_staging" {
  for_each                  = toset(module.vpc.private_route_table_ids)
  route_table_id            = each.value
  destination_cidr_block    = module.rel_staging.vpc_cidr
  vpc_peering_connection_id = aws_vpc_peering_connection.rel_staging.id
}

resource "aws_route" "rel_staging_to_eks" {
  provider = aws.eu-west-1
  # Index-keyed (not toset) so the keys stay known before the copy's route
  # tables exist. A value-keyed set fails plan and import while the VPC is unbuilt.
  for_each                  = { for idx, rt in module.rel_staging.private_route_table_ids : tostring(idx) => rt }
  route_table_id            = each.value
  destination_cidr_block    = module.vpc.vpc_cidr_block
  vpc_peering_connection_id = aws_vpc_peering_connection.rel_staging.id
}
