# Owner: mysql
# ps3.cd.percona.com is now served by the in-cluster jenkins-ps3-k8s controller
# (EKS). The classic EC2 spot master (the former module "ps3") was retired.
# Its network + worker-IAM substrate and the ARM Graviton fleet are re-parented
# into module.ps3_arm_fleet (./modules/jenkins-arm-standalone) so the in-cluster
# controller keeps its docker-32gb-aarch64 fallback, reached by private IP over
# the EKS<->ps3 peering and driven by the controller's EKS Pod Identity role.

module "ps3_arm_fleet" {
  source    = "./modules/jenkins-arm-standalone"
  providers = { aws = aws.eu-west-1 }

  short_name        = "jenkins-ps3"
  team              = "mysql"
  vpc_cidr          = "10.181.0.0/22"
  cache_bucket_name = "ps-build-cache"
  key_name          = "percona-jenkins"
  instance_types    = ["m8g.2xlarge", "m7g.2xlarge", "m6g.2xlarge", "m7gd.2xlarge", "m6gd.2xlarge", "r8g.2xlarge", "r7g.2xlarge", "r6g.2xlarge"]
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
