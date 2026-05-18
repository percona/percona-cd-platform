# Cross-region VPC peerings: percona-ci-platform EKS VPC <-> each Jenkins
# master VPC. Required so the in-cluster jenkins-ingress nginx proxy
# (resources/addons/jenkins-ingress) can reach the master's :8080 over a
# private path, instead of hairpinning through the NAT GW and the public
# internet. Once peering is in place, openresty/TLS on the master becomes
# unnecessary (PS-10945, ADR 0019).
#
# Per-master: data lookup of the remote VPC + a route table whose subnet
# hosts the master ENI, a peering connection from us-east-1, an accepter
# in the master's region, and two aws_route entries (one each side).
#
# The peering connection requires both VPC CIDRs to be unique across the
# peer pair. EKS VPC is 10.220.0.0/16; ps3 today is 10.177.0.0/22 (no
# conflict). The 4-way 10.177.0.0/22 collision (ps3, pxc, ps57, cloud)
# only matters when 2+ collidors peer to EKS at the same time; one is
# fine. Renumbering 3 of the 4 collidors is deferred to a separate
# change when adding their peerings.

# --- ps3.cd.percona.com (eu-west-1) ---

data "aws_vpc" "ps3" {
  provider = aws.eu-west-1
  filter {
    name   = "tag:iit-billing-tag"
    values = ["jenkins-ps3"]
  }
}

# The CF stack jenkins-ps3 creates two route tables: a default (unused) and
# a custom one with the IGW route + S3 endpoint. Pick the one with subnets
# associated (it routes the master's traffic).
data "aws_route_tables" "ps3" {
  provider = aws.eu-west-1
  vpc_id   = data.aws_vpc.ps3.id
  filter {
    name   = "association.subnet-id"
    values = ["*"]
  }
}

resource "aws_vpc_peering_connection" "ps3" {
  vpc_id      = module.vpc.vpc_id
  peer_vpc_id = data.aws_vpc.ps3.id
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

# EKS private subnets -> ps3 VPC via peering. Pod traffic to 10.177.0.0/22
# now goes via peering (longest-prefix match wins over the default
# 0.0.0.0/0 -> NAT route).
resource "aws_route" "eks_private_to_ps3" {
  for_each                  = toset(module.vpc.private_route_table_ids)
  route_table_id            = each.value
  destination_cidr_block    = data.aws_vpc.ps3.cidr_block
  vpc_peering_connection_id = aws_vpc_peering_connection.ps3.id
}

# ps3 subnets -> EKS VPC via peering. Replies + initiated traffic from
# master back to pods. We add to every subnet-associated route table in
# the ps3 VPC (typically just one).
resource "aws_route" "ps3_to_eks" {
  provider                  = aws.eu-west-1
  for_each                  = toset(data.aws_route_tables.ps3.ids)
  route_table_id            = each.value
  destination_cidr_block    = module.vpc.vpc_cidr_block
  vpc_peering_connection_id = aws_vpc_peering_connection.ps3.id
}
