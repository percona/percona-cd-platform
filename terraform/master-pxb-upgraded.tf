# Owner: mysql
# pxb-upgraded.cd.percona.com -- FENCED blue/green clone of pxb for the
# Jenkins core 2.568.2 + Java 21 upgrade validation (core security advisories
# 2026-06-10 and 2026-08-05). Boots production pxb's real JENKINS_HOME from a
# snapshot-restored data volume, on the upgraded core, with clouds, agents,
# triggers and executors fenced off by
# resources/jenkins-masters/pxb-upgraded/init.groovy.d/ (a-fence.groovy and
# same-name stubs overwrite the restored copies at boot), plus an explicit
# IAM deny below so the master role cannot start or terminate instances even
# if the groovy fence loses a race. Production pxb stays untouched.
#
# TEMPORARY: delete this file (and the addon host entries) after the pxb
# core upgrade lands.
#
# Data volume is adopted, not created (the ps57 retained-volume precedent, master-ps57.tf):
#   1. aws ec2 create-volume --region us-west-2 --availability-zone us-west-2b \
#        --snapshot-id <newest completed DLM snapshot of vol-08cacafbce3ce7fdd> \
#        --volume-type gp2 \
#        --tag-specifications 'ResourceType=volume,Tags=[{Key=Name,Value=jenkins-pxb-upgraded},{Key=iit-billing-tag,Value=jenkins-pxb-upgraded},{Key=PerconaKeep,Value=True}]'
#   2. tofu -chdir=terraform import module.pxb_upgraded.aws_ebs_volume.data vol-XXXX (raw-tofu exception)
#      (zero-diff adopt: size/type/AZ match and snapshot_id is in the module's
#      lifecycle ignore_changes; expect a tags-only in-place change)
#   3. just tf-apply, then upgrade all plugins on the clone and validate.
# The module's mkfs.xfs runs without -f, so the restored filesystem is never
# wiped. jenkins_home_dirname keeps JENKINS_HOME at the snapshot's directory
# name, so the restored tree is used as-is with no rename.
module "pxb_upgraded" {
  source    = "./modules/jenkins-master"
  providers = { aws = aws.us-west-2 }

  hostname             = "pxb-upgraded.cd.percona.com"
  short_name           = "jenkins-pxb-upgraded"
  team                 = "mysql"
  vpc_cidr             = "10.161.0.0/22"
  ami_id               = nonsensitive(data.aws_ssm_parameter.al2023_minimal_usw2.value) # latest AL2023 minimal (amis.tf)
  master_profile       = "eks_observability"
  ssh_allowed_cidrs    = local.master_ssh_allowed_cidrs
  launch_template_name = "PXBUpgradedMasterTemplate"

  # The upgrade under validation: LTS 2.568.2 requires Java 21 on the
  # controller JVM (policy since 2.555.1). Production masters keep the module
  # defaults (2.541.3 on the Java 17 package) until this clone proves the jump.
  jenkins_package_version = "2.568.2"
  java_package            = "java-21-amazon-corretto-headless"

  # JENKINS_HOME stays at the snapshot's directory name (/mnt/pxb.cd.percona.com)
  # so the restored home is adopted without a rename. The Alloy master label
  # still reports this module's hostname, so clone telemetry never pollutes
  # production pxb's series.
  jenkins_home_dirname = "pxb.cd.percona.com"

  # Same volume geometry as the snapshot source (250 GiB gp2 in us-west-2b).
  ebs_size = 250
  ebs_type = "gp2"
  az_index = 1

  purchasing_option       = "on-demand"
  on_demand_instance_type = "m7i-flex.large"

  create_eip      = false
  master_key_name = "percona-jenkins"

  extra_master_managed_policies = [
    "arn:aws:iam::aws:policy/CloudWatchAgentServerPolicy",
  ]

  extra_master_inline_policies = [
    {
      name = "AlloyGatewayBearerRead"
      json = data.aws_iam_policy_document.pxb_upgraded_alloy_bearer_read.json
    },
    # Fail-closed fence: an explicit deny beats the module's StartInstances
    # allow, so even a boot-race window where the restored config.xml still
    # lists production pxb's clouds cannot run or terminate EC2 instances.
    {
      name = "DenyWorkerProvisioning"
      json = data.aws_iam_policy_document.pxb_upgraded_deny_provisioning.json
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

  # Fence set, NOT pxb's files: a-fence.groovy clears clouds/agents/executors
  # and fixes the URL, same-name stubs neutralize the restored cloud.groovy,
  # htz.cloud.groovy, ec2FleetCloud.groovy, hetzner* and matrix.groovy, and
  # disable-triggers.groovy strips every cron/SCM trigger.
  init_groovy_files = {
    for f in fileset("${path.module}/../resources/jenkins-masters/pxb-upgraded/init.groovy.d", "*.groovy") :
    f => file("${path.module}/../resources/jenkins-masters/pxb-upgraded/init.groovy.d/${f}")
  }
  init_groovy_sync_schedule = "rate(30 minutes)"
}

# No arm-fleet sibling: the clone provisions no workers, and the shared ASG
# (jenkins-pxb-arm-graviton) belongs to production pxb.

data "aws_iam_policy_document" "pxb_upgraded_alloy_bearer_read" {
  statement {
    sid       = "AlloyGatewayBearerRead"
    effect    = "Allow"
    actions   = ["secretsmanager:GetSecretValue"]
    resources = ["arn:aws:secretsmanager:us-east-1:${data.aws_caller_identity.current.account_id}:secret:percona-ci-platform/alloy-gateway/bearer-*"]
  }
}

data "aws_iam_policy_document" "pxb_upgraded_deny_provisioning" {
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

# Cross-region VPC peering EKS (us-east-1) <-> pxb-upgraded (us-west-2),
# so the in-cluster jenkins-ingress nginx can reach the master's private IP on
# :8080. Mirrors the pxb peering. CIDRs: EKS 10.220.0.0/16, clone 10.161.0.0/22.

resource "aws_vpc_peering_connection" "pxb_upgraded" {
  vpc_id      = module.vpc.vpc_id
  peer_vpc_id = module.pxb_upgraded.vpc_id
  peer_region = "us-west-2"
  auto_accept = false

  tags = {
    Name = "${local.cluster_name}-to-jenkins-pxb-upgraded"
  }
}

resource "aws_vpc_peering_connection_accepter" "pxb_upgraded" {
  provider                  = aws.us-west-2
  vpc_peering_connection_id = aws_vpc_peering_connection.pxb_upgraded.id
  auto_accept               = true

  tags = {
    Name = "${local.cluster_name}-to-jenkins-pxb-upgraded"
  }
}

resource "aws_route" "eks_private_to_pxb_upgraded" {
  for_each                  = toset(module.vpc.private_route_table_ids)
  route_table_id            = each.value
  destination_cidr_block    = module.pxb_upgraded.vpc_cidr
  vpc_peering_connection_id = aws_vpc_peering_connection.pxb_upgraded.id
}

resource "aws_route" "pxb_upgraded_to_eks" {
  provider = aws.us-west-2
  # Index-keyed (not toset) so the keys stay known before the clone's route
  # tables exist; a value-keyed set fails plan/import while the VPC is unbuilt.
  for_each                  = { for idx, rt in module.pxb_upgraded.private_route_table_ids : tostring(idx) => rt }
  route_table_id            = each.value
  destination_cidr_block    = module.vpc.vpc_cidr_block
  vpc_peering_connection_id = aws_vpc_peering_connection.pxb_upgraded.id
}
