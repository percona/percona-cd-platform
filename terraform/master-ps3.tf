# ps3.cd.percona.com is now served by the in-cluster jenkins-ps3-k8s controller
# (EKS). The classic EC2 spot master (the former module "ps3") is being retired.
# Its network + worker-IAM substrate and the ARM Graviton fleet are re-parented
# into module.ps3_arm_fleet (./modules/jenkins-arm-standalone) so the in-cluster
# controller keeps its docker-32gb-aarch64 fallback, reached by private IP over
# the EKS<->ps3 peering and driven by the controller's EKS Pod Identity role.
#
# The moved{} blocks relocate the substrate + worker IAM with zero diff (state
# re-key, not recreate); the removed{} block forgets the JENKINS_HOME EBS without
# destroying it; the master-only resources (spot fleet, launch template, master
# role + SGs, init bucket, SQS) are destroyed when the old module block is gone.

module "ps3_arm_fleet" {
  source    = "./modules/jenkins-arm-standalone"
  providers = { aws = aws.eu-west-1 }

  short_name        = "jenkins-ps3"
  vpc_cidr          = "10.181.0.0/22"
  cache_bucket_name = "ps-build-cache"
  key_name          = "percona-jenkins"
  instance_types    = ["m8g.2xlarge", "m7g.2xlarge", "m6g.2xlarge"]
  max_size          = 16

  # The in-cluster ps3-k8s controller reaches Fleet workers by private IP over the
  # EKS<->ps3 VPC peering (ec2-fleet privateIpUsed), so allow SSH from the EKS VPC.
  extra_ssh_cidrs = [module.vpc.vpc_cidr_block]
  tickets         = "PS-11179"
}

# Cross-region VPC peering EKS (us-east-1) <-> ps3 (eu-west-1). The ps3 side VPC
# is now owned by module.ps3_arm_fleet (re-parented from the retired master).
resource "aws_vpc_peering_connection" "ps3" {
  vpc_id      = module.vpc.vpc_id
  peer_vpc_id = module.ps3_arm_fleet.vpc_id
  peer_region = "eu-west-1"
  auto_accept = false

  tags = {
    Name = "${local.cluster_name}-to-jenkins-ps3"
  }
}

resource "aws_vpc_peering_connection_accepter" "ps3" {
  provider                  = aws.eu-west-1
  vpc_peering_connection_id = aws_vpc_peering_connection.ps3.id
  auto_accept               = true

  tags = {
    Name = "${local.cluster_name}-to-jenkins-ps3"
  }
}

resource "aws_route" "eks_private_to_ps3" {
  for_each                  = toset(module.vpc.private_route_table_ids)
  route_table_id            = each.value
  destination_cidr_block    = module.ps3_arm_fleet.vpc_cidr
  vpc_peering_connection_id = aws_vpc_peering_connection.ps3.id
}

resource "aws_route" "ps3_to_eks" {
  provider                  = aws.eu-west-1
  for_each                  = toset(module.ps3_arm_fleet.private_route_table_ids)
  route_table_id            = each.value
  destination_cidr_block    = module.vpc.vpc_cidr_block
  vpc_peering_connection_id = aws_vpc_peering_connection.ps3.id
}

# ----- one-time re-parent scaffolding (delete in a follow-up once applied) -----
# Relocate the substrate + worker IAM from the retired master into the standalone
# ARM module. `moved` is a state re-key: the resources are preserved, not recreated.
moved {
  from = module.ps3.aws_vpc.this
  to   = module.ps3_arm_fleet.aws_vpc.this
}
moved {
  from = module.ps3.aws_internet_gateway.this
  to   = module.ps3_arm_fleet.aws_internet_gateway.this
}
moved {
  from = module.ps3.aws_subnet.this["b"]
  to   = module.ps3_arm_fleet.aws_subnet.this["b"]
}
moved {
  from = module.ps3.aws_subnet.this["c"]
  to   = module.ps3_arm_fleet.aws_subnet.this["c"]
}
moved {
  from = module.ps3.aws_route_table.this
  to   = module.ps3_arm_fleet.aws_route_table.this
}
moved {
  from = module.ps3.aws_route.internet
  to   = module.ps3_arm_fleet.aws_route.internet
}
moved {
  from = module.ps3.aws_route_table_association.this["b"]
  to   = module.ps3_arm_fleet.aws_route_table_association.this["b"]
}
moved {
  from = module.ps3.aws_route_table_association.this["c"]
  to   = module.ps3_arm_fleet.aws_route_table_association.this["c"]
}
moved {
  from = module.ps3.aws_vpc_endpoint.s3
  to   = module.ps3_arm_fleet.aws_vpc_endpoint.s3
}
moved {
  from = module.ps3.aws_iam_role.worker
  to   = module.ps3_arm_fleet.aws_iam_role.worker
}
moved {
  from = module.ps3.aws_iam_role_policy.worker
  to   = module.ps3_arm_fleet.aws_iam_role_policy.worker
}
moved {
  from = module.ps3.aws_iam_instance_profile.worker
  to   = module.ps3_arm_fleet.aws_iam_instance_profile.worker
}

# Forget the JENKINS_HOME data volume without destroying it. It has prevent_destroy
# and PerconaKeep=True; it detaches to `available` when the spot master terminates
# and is retained (plus the pre-decommission snapshot snap-07e2b31bc3c01241a).
removed {
  from = module.ps3.aws_ebs_volume.data
  lifecycle {
    destroy = false
  }
}
