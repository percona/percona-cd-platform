# Owner: pxc
# pxc.cd.percona.com -- Terraform-managed via ./modules/jenkins-master/.
# Migrated from CloudFormation stack jenkins-pxc
# (Percona-Lab/jenkins-pipelines/IaC/pxc.cd).
#
# Same cutover as ps3/ps80/ps57/pxb: off the AWS SpotFleet onto a single
# on-demand instance (purchasing_option), EKS-fronted (TLS terminates at the
# jenkins-masters ALB, reached over cross-region VPC peering on :8080),
# declarative init.groovy.d via the module S3 bucket, MAX_SURVIVABILITY +
# Hetzner worker rehydration, and the Graviton ARM EC2 Fleet fallback.
#
# pxc-specific deltas: region us-west-1; a fresh VPC at 10.156.0.0/22 because
# the live CFN VPC (10.177.0.0/22) collides with cloud and cannot peer to the
# EKS hub (re-CIDR variant, like ps57). Moving pxc off 10.177 lets cloud keep
# it as the sole occupant later. The retained 300 GiB gp2 data volume is
# imported in us-west-1b. On-demand m7i-flex.large (2 vCPU / 8 GB): the live
# master averages 0.63% CPU (p99 3.37%) and the 8 GB box runs ~4.8 GB used, so
# 8 GB holds the -Xmx heap and 2 flex-burst vCPU cover the rare spikes.
module "pxc" {
  source    = "./modules/jenkins-master"
  providers = { aws = aws.us-west-1 }

  hostname                = "pxc.cd.percona.com"
  short_name              = "jenkins-pxc"
  team                    = "pxc"
  vpc_cidr                = "10.156.0.0/22"
  ami_id                  = nonsensitive(data.aws_ssm_parameter.al2023_minimal_usw1.value) # latest AL2023 minimal (amis.tf)
  master_profile          = "eks_observability"
  ssh_allowed_cidrs       = local.master_ssh_allowed_cidrs
  jenkins_package_version = "2.541.3"
  cache_bucket_name       = "pxc-build-cache"

  # Retained CFN data volume vol-03b3852ad6dd6c553 is 300 GiB gp2 in us-west-1b
  # (the live volume; the CFN template still declares 100 GiB). ebs_type must be
  # gp2 (not the module default gp3) and az_index 1 (us-west-1b) so the tofu
  # import is a zero-diff adopt and the on-demand instance lands in the volume's
  # AZ (EBS is AZ-bound). NOTE: aws_availability_zones.available.names[1] must
  # resolve to us-west-1b in this account; hard-gate that before apply.
  ebs_size = 300
  ebs_type = "gp2"
  az_index = 1

  # On-demand to end the spot reclamations. m7i-flex.large is 2 vCPU / 8 GB:
  # matches the prior c5a.xlarge memory footprint (CPU is idle), and clears the
  # master JVM heap.
  purchasing_option       = "on-demand"
  on_demand_instance_type = "m7i-flex.large"

  # Distinct LT name so the module's launch template can coexist with the
  # CFN-created PXCMasterTemplate during the cutover (no name collision,
  # order-independent of delete-stack). The instance references the LT by ID.
  launch_template_name = "PXCMasterTemplateTF"

  # EKS-fronted for HTTP: DNS is external-dns -> ALB -> private IP over peering,
  # admin via paws/SSM. NOT EIP-less (unlike ps3/ps80): create_eip=true is a
  # REMOVABLE WORKAROUND that keeps the public IP 13.56.198.107 for the CHAOS
  # team's `test-chaos-vm` direct-TCP inbound JNLP agent (:50000 from the corp DC
  # range 40.143.89.204/30, set up by IT in Feb 2026). jnlpHost.groovy advertises
  # this EIP as the inbound-agent host so the agent connects transparently. REMOVE
  # when test-chaos-vm is retired or moved to WebSocket: set create_eip=false,
  # delete jnlpHost.groovy + the EIP retain, then -replace the instance.
  create_eip      = true
  master_key_name = "percona-jenkins"

  extra_master_managed_policies = [
    "arn:aws:iam::aws:policy/CloudWatchAgentServerPolicy",
  ]

  extra_master_inline_policies = [
    {
      name = "AlloyGatewayBearerRead"
      json = data.aws_iam_policy_document.pxc_alloy_bearer_read.json
    },
  ]

  # :8080 from the EKS VPC over cross-region peering for the jenkins-ingress
  # nginx (TLS offloaded at the ALB). :50000 from the CHAOS team CIDR preserves
  # the inbound-JNLP-agent path the live master's non-CFN chaos-jenkins SG
  # carried (the module otherwise creates only SSH + HTTP SGs).
  extra_http_ingress = [
    { port = 8080, cidr = module.vpc.vpc_cidr_block },
    { port = 50000, cidr = "40.143.89.204/30" },
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

  # Declarative init.groovy.d delivered via the
  # module-created S3 bucket (jenkins-pxc-init-config). Each file under
  # resources/jenkins-masters/pxc/init.groovy.d/ is uploaded by Terraform and
  # pulled at boot, self-healing a fresh-volume rebuild. cloud.groovy is
  # simplified to pxc's used worker labels (map-driven, gp3 volumes),
  # htz.cloud.groovy carries the standard fleet Hetzner template set (x64 +
  # CAX aarch64, same shape as the other masters), ec2FleetCloud points at
  # the Graviton ASG. `jenkins iac deploy` stays the no-restart hot-reload path.
  init_groovy_files = {
    for f in fileset("${path.module}/../resources/jenkins-masters/pxc/init.groovy.d", "*.groovy") :
    f => file("${path.module}/../resources/jenkins-masters/pxc/init.groovy.d/${f}")
  }
  init_groovy_sync_schedule = "rate(30 minutes)"
}

# ARM Graviton spot fleet for the ec2-fleet plugin -- the
# docker-aarch64 fallback. capacity-optimized across m8g/m7g/m6g.4xlarge.
# ec2FleetCloud.groovy points pxc's EC2FleetCloud at this ASG
# (jenkins-pxc-arm-graviton) on the docker-32gb-aarch64 label. $0 idle
# (min_size/desired 0); the ec2-fleet plugin drives DesiredCapacity. Consumes
# the TF master's VPC/subnets/role outputs.
module "pxc_arm_fleet" {
  source    = "./modules/jenkins-arm-fleet"
  providers = { aws = aws.us-west-1 }

  short_name                   = "jenkins-pxc"
  team                         = "pxc"
  vpc_id                       = module.pxc.vpc_id
  subnet_ids                   = module.pxc.subnet_ids
  worker_instance_profile_name = module.pxc.worker_instance_profile_name
  master_role_name             = module.pxc.master_iam_role_name
  key_name                     = "percona-jenkins"
  instance_types               = ["m8g.4xlarge", "m7g.4xlarge", "m6g.4xlarge", "m7gd.4xlarge", "m6gd.4xlarge", "r8g.4xlarge", "r7g.4xlarge", "r6g.4xlarge"]
  max_size                     = 16
  tickets                      = "PS-11228"
}

# Rendered in the root so the ARN uses this account's caller-identity, not the
# us-west-1 alias's. Secret lives in us-east-1 with alloy-gateway.
data "aws_iam_policy_document" "pxc_alloy_bearer_read" {
  statement {
    sid       = "AlloyGatewayBearerRead"
    effect    = "Allow"
    actions   = ["secretsmanager:GetSecretValue"]
    resources = ["arn:aws:secretsmanager:us-east-1:${data.aws_caller_identity.current.account_id}:secret:percona-ci-platform/alloy-gateway/bearer-*"]
  }
}

# Cross-region VPC peering EKS (us-east-1) <-> pxc (us-west-1), so the
# in-cluster jenkins-ingress nginx can reach the master's private IP on :8080
# (TLS offloaded at the jenkins-masters ALB). CIDRs are distinct: EKS
# 10.220.0.0/16, pxc 10.156.0.0/22.

resource "aws_vpc_peering_connection" "pxc" {
  vpc_id      = module.vpc.vpc_id
  peer_vpc_id = module.pxc.vpc_id
  peer_region = "us-west-1"
  auto_accept = false

  tags = {
    Name = "${local.cluster_name}-to-jenkins-pxc"
  }
}

resource "aws_vpc_peering_connection_accepter" "pxc" {
  provider                  = aws.us-west-1
  vpc_peering_connection_id = aws_vpc_peering_connection.pxc.id
  auto_accept               = true

  tags = {
    Name = "${local.cluster_name}-to-jenkins-pxc"
  }
}

resource "aws_route" "eks_private_to_pxc" {
  for_each                  = toset(module.vpc.private_route_table_ids)
  route_table_id            = each.value
  destination_cidr_block    = module.pxc.vpc_cidr
  vpc_peering_connection_id = aws_vpc_peering_connection.pxc.id
}

resource "aws_route" "pxc_to_eks" {
  provider                  = aws.us-west-1
  for_each                  = toset(module.pxc.private_route_table_ids)
  route_table_id            = each.value
  destination_cidr_block    = module.vpc.vpc_cidr_block
  vpc_peering_connection_id = aws_vpc_peering_connection.pxc.id
}
