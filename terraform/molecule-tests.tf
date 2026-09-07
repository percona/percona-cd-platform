# Owner: platform
# Dedicated VPCs for the molecule package-testing EC2 VMs that the QA
# pipelines (Percona-QA/package-testing and friends) create at job runtime.
# The scenario configs hardcode these subnet IDs in `vpc_subnet_id`, so the
# subnets must outlive any Jenkins master VPC: the 2026-06-10 master VPC
# replacement deleted a subnet that ~300 scenario files referenced and broke
# the PS/PDPS release-day jobs. Both VPCs predate this file (hand-created
# during the ps80/pxc master migrations) and are adopted here via import
# blocks, byte-exact to live, plus the account-mandatory protection tags
# (see docs/runbooks/cleanup-reapers.md) so tag-keyed cleanup automation can
# never classify them as orphans.
#
# Deliberately NOT managed here: the molecule-* security groups (created and
# owned by the playbooks' create.yml at runtime), the default SG/NACL/DHCP
# options, the empty main route table in the ps80 VPC, and the stray
# launch-wizard-8 SG (console cruft; delete manually once confirmed unused).
# The import blocks are idempotent no-ops once state holds the addresses.

# --- us-west-2 (used by the us-west-2 scenarios; subnet -b is the canonical
# --- vpc_subnet_id in package-testing after the 2026-06-10 swap) -----------

locals {
  molecule_tests_ps80_subnets = {
    b = { az = "us-west-2b", cidr = "10.177.1.0/24" }
    c = { az = "us-west-2c", cidr = "10.177.2.0/24" }
  }
}

import {
  to = aws_vpc.molecule_tests_ps80
  id = "vpc-09404e61bed281727"
}

resource "aws_vpc" "molecule_tests_ps80" {
  provider             = aws.us-west-2
  cidr_block           = "10.177.0.0/22"
  enable_dns_support   = true
  enable_dns_hostnames = false
  instance_tenancy     = "default"

  tags = { Name = "molecule-tests-ps80" }

  lifecycle {
    prevent_destroy = true
  }
}

import {
  to = aws_subnet.molecule_tests_ps80["b"]
  id = "subnet-0430e63d7cdbcd237"
}

import {
  to = aws_subnet.molecule_tests_ps80["c"]
  id = "subnet-090f054eee4908244"
}

resource "aws_subnet" "molecule_tests_ps80" {
  for_each = local.molecule_tests_ps80_subnets
  provider = aws.us-west-2

  vpc_id                  = aws_vpc.molecule_tests_ps80.id
  cidr_block              = each.value.cidr
  availability_zone       = each.value.az
  map_public_ip_on_launch = true

  tags = { Name = "molecule-tests-ps80-${each.key}" }

  lifecycle {
    prevent_destroy = true
  }
}

import {
  to = aws_internet_gateway.molecule_tests_ps80
  id = "igw-0241e26c9f6aefba9"
}

resource "aws_internet_gateway" "molecule_tests_ps80" {
  provider = aws.us-west-2
  vpc_id   = aws_vpc.molecule_tests_ps80.id

  tags = { Name = "molecule-tests-ps80" }
}

import {
  to = aws_route_table.molecule_tests_ps80
  id = "rtb-0043b29e4a8dd0f27"
}

resource "aws_route_table" "molecule_tests_ps80" {
  provider = aws.us-west-2
  vpc_id   = aws_vpc.molecule_tests_ps80.id

  tags = { Name = "molecule-tests-ps80" }
}

import {
  to = aws_route.molecule_tests_ps80_internet
  id = "rtb-0043b29e4a8dd0f27_0.0.0.0/0"
}

resource "aws_route" "molecule_tests_ps80_internet" {
  provider               = aws.us-west-2
  route_table_id         = aws_route_table.molecule_tests_ps80.id
  destination_cidr_block = "0.0.0.0/0"
  gateway_id             = aws_internet_gateway.molecule_tests_ps80.id
}

import {
  to = aws_route_table_association.molecule_tests_ps80["b"]
  id = "subnet-0430e63d7cdbcd237/rtb-0043b29e4a8dd0f27"
}

import {
  to = aws_route_table_association.molecule_tests_ps80["c"]
  id = "subnet-090f054eee4908244/rtb-0043b29e4a8dd0f27"
}

resource "aws_route_table_association" "molecule_tests_ps80" {
  for_each = local.molecule_tests_ps80_subnets
  provider = aws.us-west-2

  subnet_id      = aws_subnet.molecule_tests_ps80[each.key].id
  route_table_id = aws_route_table.molecule_tests_ps80.id
}

# --- us-west-1 (used by the pxc-flavoured scenarios; single -b subnet) ------
# Live quirks preserved: the subnet does NOT auto-assign public IPs (the
# playbooks set assign_public_ip per ENI), and the public route lives on the
# VPC's MAIN route table with an explicit subnet association. Leading-space
# Name tags from the hand-made originals are normalized here (in-place tag
# update only).

import {
  to = aws_vpc.molecule_tests_pxc
  id = "vpc-0325a86827e556145"
}

resource "aws_vpc" "molecule_tests_pxc" {
  provider             = aws.us-west-1
  cidr_block           = "10.177.0.0/22"
  enable_dns_support   = true
  enable_dns_hostnames = false
  instance_tenancy     = "default"

  tags = { Name = "molecule-tests-pxc" }

  lifecycle {
    prevent_destroy = true
  }
}

import {
  to = aws_subnet.molecule_tests_pxc
  id = "subnet-04a8ad1b1d4da874c"
}

resource "aws_subnet" "molecule_tests_pxc" {
  provider = aws.us-west-1

  vpc_id                  = aws_vpc.molecule_tests_pxc.id
  cidr_block              = "10.177.1.0/24"
  availability_zone       = "us-west-1b"
  map_public_ip_on_launch = false

  tags = { Name = "molecule-tests-pxc-1b" }

  lifecycle {
    prevent_destroy = true
  }
}

import {
  to = aws_internet_gateway.molecule_tests_pxc
  id = "igw-0ae7733fd0354a0d2"
}

resource "aws_internet_gateway" "molecule_tests_pxc" {
  provider = aws.us-west-1
  vpc_id   = aws_vpc.molecule_tests_pxc.id

  tags = { Name = "molecule-tests-pxc" }
}

# Main route table adopted in place (aws_default_route_table takes ownership
# without import); carries the public route, matching live.
resource "aws_default_route_table" "molecule_tests_pxc" {
  provider               = aws.us-west-1
  default_route_table_id = aws_vpc.molecule_tests_pxc.default_route_table_id

  route {
    cidr_block = "0.0.0.0/0"
    gateway_id = aws_internet_gateway.molecule_tests_pxc.id
  }

  tags = { Name = "molecule-tests-pxc" }
}

import {
  to = aws_route_table_association.molecule_tests_pxc
  id = "subnet-04a8ad1b1d4da874c/rtb-0510233a1dd12aeeb"
}

resource "aws_route_table_association" "molecule_tests_pxc" {
  provider = aws.us-west-1

  subnet_id      = aws_subnet.molecule_tests_pxc.id
  route_table_id = aws_default_route_table.molecule_tests_pxc.id
}

# --- Runtime molecule security groups, both VPCs ----------------------------
# Created originally by the playbooks' create.yml (amazon.aws.ec2_group) at
# job runtime and re-asserted on every run with purge_rules enabled, so the
# rule sets below MUST stay an exact mirror of create.yml: a rule added only
# here gets purged by the next job, a rule added only in create.yml gets
# purged by the next apply. Adopted for tagging and drift visibility; the
# playbooks keep converging to the same fixpoint.

locals {
  molecule_tests_ps80_runtime_sgs = {
    "molecule-vpc-09404e61bed281727" = {
      id   = "sg-0284e8486648b8d12"
      desc = "Security group for testing Molecule"
    }
    "molecule-ps-innodb-cluster" = {
      id   = "sg-0820834c707e31148"
      desc = "Testing PS InnoDB Cluster with Molecule"
    }
    "molecule-pxc-package-testing" = {
      id   = "sg-02c97bd8e1899c44f"
      desc = "Testing PXC package testing with Molecule"
    }
    "molecule-pxc-package-testing-vpc-09404e61bed281727" = {
      id   = "sg-0e7585094b75622af"
      desc = "Testing PXC package testing with Molecule"
    }
  }
  molecule_tests_pxc_runtime_sgs = {
    "molecule-vpc-0325a86827e556145" = {
      id   = "sg-0813ce235cb4a73eb"
      desc = "Security group for testing Molecule"
    }
    "molecule-pxc-package-testing-vpc-0325a86827e556145" = {
      id   = "sg-09a70f3629beb203b"
      desc = "Testing PXC package testing with Molecule"
    }
  }
}

import {
  for_each = local.molecule_tests_ps80_runtime_sgs
  to       = aws_security_group.molecule_tests_ps80_runtime[each.key]
  id       = each.value.id
}

# Workers SSH to the test VMs over their public IPs from arbitrary egress
# points (AWS workers, Hetzner, ARM fleet), so 22/tcp and ICMP echo stay
# world-open by design; mirrors create.yml exactly (see header above).
#trivy:ignore:AVD-AWS-0107
resource "aws_security_group" "molecule_tests_ps80_runtime" {
  for_each = local.molecule_tests_ps80_runtime_sgs
  provider = aws.us-west-2

  name        = each.key
  description = each.value.desc
  vpc_id      = aws_vpc.molecule_tests_ps80.id

  ingress {
    protocol  = "-1"
    from_port = 0
    to_port   = 0
    self      = true
  }
  ingress {
    protocol    = "tcp"
    from_port   = 22
    to_port     = 22
    cidr_blocks = ["0.0.0.0/0"]
  }
  ingress {
    protocol    = "icmp"
    from_port   = 8
    to_port     = -1
    cidr_blocks = ["0.0.0.0/0"]
  }
  egress {
    protocol    = "-1"
    from_port   = 0
    to_port     = 0
    cidr_blocks = ["0.0.0.0/0"]
  }
}

import {
  for_each = local.molecule_tests_pxc_runtime_sgs
  to       = aws_security_group.molecule_tests_pxc_runtime[each.key]
  id       = each.value.id
}

# Workers SSH to the test VMs over their public IPs from arbitrary egress
# points (AWS workers, Hetzner, ARM fleet), so 22/tcp and ICMP echo stay
# world-open by design; mirrors create.yml exactly (see header above).
#trivy:ignore:AVD-AWS-0107
resource "aws_security_group" "molecule_tests_pxc_runtime" {
  for_each = local.molecule_tests_pxc_runtime_sgs
  provider = aws.us-west-1

  name        = each.key
  description = each.value.desc
  vpc_id      = aws_vpc.molecule_tests_pxc.id

  ingress {
    protocol  = "-1"
    from_port = 0
    to_port   = 0
    self      = true
  }
  ingress {
    protocol    = "tcp"
    from_port   = 22
    to_port     = 22
    cidr_blocks = ["0.0.0.0/0"]
  }
  ingress {
    protocol    = "icmp"
    from_port   = 8
    to_port     = -1
    cidr_blocks = ["0.0.0.0/0"]
  }
  egress {
    protocol    = "-1"
    from_port   = 0
    to_port     = 0
    cidr_blocks = ["0.0.0.0/0"]
  }
}
