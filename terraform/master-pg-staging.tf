# Owner: postgresql
# pg-staging.cd.percona.com -- staging copy of pg for the Jenkins core
# 2.568.3 + Java 21 upgrade. Boots production pg's
# real JENKINS_HOME from a snapshot-restored data volume on the upgraded core.
# resources/jenkins-masters/pg-staging/init.groovy.d/ clears clouds, agents,
# triggers and executors at boot (a-fence.groovy plus same-name stubs), and
# the IAM deny below stops the master role from starting or terminating
# instances even if a script loses a boot race. Production pg stays untouched.
#
# TEMPORARY: delete this file, the fence set and the addon host entries after
# the production pg roll.
#
# Data volume is adopted, not created:
#   1. aws ec2 create-volume --region eu-central-1 --availability-zone eu-central-1b \
#        --snapshot-id <newest completed DLM snapshot of production pg's data volume> \
#        --volume-type gp3 \
#        --tag-specifications 'ResourceType=volume,Tags=[{Key=Name,Value=jenkins-pg-staging},{Key=iit-billing-tag,Value=jenkins-pg-staging},{Key=PerconaKeep,Value=True}]'
#   2. tofu -chdir=terraform import module.pg_staging.aws_ebs_volume.data vol-XXXX (raw-tofu exception)
#   3. just tf-apply.
# The module's mkfs.xfs runs without -f, so the restored filesystem is never
# wiped. jenkins_home_dirname keeps JENKINS_HOME at the snapshot's directory
# name, so the restored tree is used as-is.
module "pg_staging" {
  source    = "./modules/jenkins-master"
  providers = { aws = aws.eu-central-1 }

  hostname             = "pg-staging.cd.percona.com"
  short_name           = "jenkins-pg-staging"
  team                 = "postgresql"
  vpc_cidr             = "10.160.0.0/22"
  ami_id               = data.aws_ami.al2023_minimal_euc1.id # pinned AL2023 minimal (amis.tf)
  master_profile       = "eks_observability"
  ssh_allowed_cidrs    = local.master_ssh_allowed_cidrs
  engineer_roster      = local.master_ssh_engineer_roster
  cache_bucket_name    = "pg-build-cache"
  launch_template_name = "PGStagingMasterTemplate"

  # The upgrade under test. Production pg keeps its current core until its roll.
  jenkins_package_version = "2.568.3"
  java_package            = "java-21-amazon-corretto-headless"
  jvm_memory_opts         = "-Xms3072m -Xmx4096m -Xss4m"

  # JENKINS_HOME stays at the snapshot's directory name (/mnt/pg.cd.percona.com).
  # The Alloy master label still reports this module's hostname, so staging
  # telemetry never mixes with production pg's series.
  jenkins_home_dirname = "pg.cd.percona.com"

  # Same volume geometry as production pg (500 GiB gp3 in eu-central-1b).
  ebs_size = 500
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
      json = data.aws_iam_policy_document.pg_staging_alloy_bearer_read.json
    },
    # Explicit deny beats the module's StartInstances allow, so a boot-race
    # window where the restored config.xml still lists production clouds
    # cannot run or terminate EC2 instances.
    {
      name = "DenyWorkerProvisioning"
      json = data.aws_iam_policy_document.pg_staging_deny_provisioning.json
    },
    # Read its own staging-admin password (terraform/staging-admin.tf).
    {
      name = "StagingAdminPasswordRead"
      json = data.aws_iam_policy_document.pg_staging_admin_read.json
    },
  ]

  # :8080 from the EKS VPC over cross-region peering for the jenkins-ingress
  # nginx (TLS offloaded at the ALB).
  extra_http_ingress = [
    { port = 8080, cidr = module.vpc.vpc_cidr_block },
  ]

  # Fence set, NOT production pg's files.
  init_groovy_files = {
    for f in fileset("${path.module}/../resources/jenkins-masters/pg-staging/init.groovy.d", "*.groovy") :
    f => file("${path.module}/../resources/jenkins-masters/pg-staging/init.groovy.d/${f}")
  }
  init_groovy_sync_schedule = "rate(30 minutes)"
}

# No arm-fleet sibling: the staging copy provisions no workers, and the ASG
# jenkins-pg-arm-graviton belongs to production pg.

data "aws_iam_policy_document" "pg_staging_alloy_bearer_read" {
  statement {
    sid       = "AlloyGatewayBearerRead"
    effect    = "Allow"
    actions   = ["secretsmanager:GetSecretValue"]
    resources = ["arn:aws:secretsmanager:us-east-1:${data.aws_caller_identity.current.account_id}:secret:percona-ci-platform/alloy-gateway/bearer-*"]
  }
}

data "aws_iam_policy_document" "pg_staging_admin_read" {
  statement {
    sid       = "StagingAdminPasswordRead"
    effect    = "Allow"
    actions   = ["ssm:GetParameter"]
    resources = [aws_ssm_parameter.staging_admin_password["pg"].arn]
  }
}

data "aws_iam_policy_document" "pg_staging_deny_provisioning" {
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

# Cross-region VPC peering EKS (us-east-1) <-> pg-staging (eu-central-1), so the
# in-cluster jenkins-ingress nginx can reach the master's private IP on :8080.
# CIDRs: EKS 10.220.0.0/16, pg-staging 10.160.0.0/22.

resource "aws_vpc_peering_connection" "pg_staging" {
  vpc_id      = module.vpc.vpc_id
  peer_vpc_id = module.pg_staging.vpc_id
  peer_region = "eu-central-1"
  auto_accept = false

  tags = {
    Name = "${local.cluster_name}-to-jenkins-pg-staging"
  }
}

resource "aws_vpc_peering_connection_accepter" "pg_staging" {
  provider                  = aws.eu-central-1
  vpc_peering_connection_id = aws_vpc_peering_connection.pg_staging.id
  auto_accept               = true

  tags = {
    Name = "${local.cluster_name}-to-jenkins-pg-staging"
  }
}

resource "aws_route" "eks_private_to_pg_staging" {
  for_each                  = toset(module.vpc.private_route_table_ids)
  route_table_id            = each.value
  destination_cidr_block    = module.pg_staging.vpc_cidr
  vpc_peering_connection_id = aws_vpc_peering_connection.pg_staging.id
}

resource "aws_route" "pg_staging_to_eks" {
  provider = aws.eu-central-1
  # Index-keyed (not toset) so the keys stay known before the copy's route
  # tables exist. A value-keyed set fails plan and import while the VPC is unbuilt.
  for_each                  = { for idx, rt in module.pg_staging.private_route_table_ids : tostring(idx) => rt }
  route_table_id            = each.value
  destination_cidr_block    = module.vpc.vpc_cidr_block
  vpc_peering_connection_id = aws_vpc_peering_connection.pg_staging.id
}
