# ps3.cd.percona.com — first master moved from CloudFormation
# (Percona-Lab/jenkins-pipelines/IaC/ps3.cd) to this repo via
# ./modules/jenkins-master/. PS-10945 + plan validated-herding-stardust.md.
#
# EIP eipalloc-081a07ed2e87a8f74 / 52.210.70.55 is intentionally not
# imported: the ALB owns public HTTPS, SSM Session Manager owns SSH, so
# the EIP is dead weight. Released in a follow-up apply after the CF
# stack is deleted.

module "ps3" {
  source    = "./modules/jenkins-master"
  providers = { aws = aws.eu-west-1 }

  hostname                = "ps3.cd.percona.com"
  short_name              = "jenkins-ps3"
  vpc_cidr                = "10.181.0.0/22"
  ami_id                  = "ami-0c27e7b62088f2235" # Amazon Linux 2023 in eu-west-1 (matches CF Mapping)
  spot_instance_types     = ["c7i-flex.xlarge", "m7i-flex.xlarge", "c5a.xlarge", "c5d.xlarge"]
  spot_price              = "0.17"
  master_profile          = "eks_observability"
  jenkins_package_version = "2.541.3"
  cache_bucket_name       = "ps-build-cache"
  create_eip              = false # ALB owns HTTPS, SSM owns SSH
  create_route53_record   = false # external-dns owns ps3.cd.percona.com

  # CloudWatch policy is vestigial after the Alloy push migration; kept
  # attached to keep import diff zero, detached in a follow-up.
  extra_master_managed_policies = [
    "arn:aws:iam::aws:policy/CloudWatchAgentServerPolicy",
    "arn:aws:iam::${data.aws_caller_identity.current.account_id}:policy/ec2.DescribeSpotPriceHistory",
  ]

  extra_master_inline_policies = [
    {
      name = "AlloyGatewayBearerRead"
      json = data.aws_iam_policy_document.ps3_alloy_bearer_read.json
    },
  ]

  # :8080 from EKS VPC over cross-region peering for jenkins-ingress nginx.
  extra_http_ingress = [
    { port = 8080, cidr = module.vpc.vpc_cidr_block },
  ]

  ssh_key_engineers = [
    "anderson.nogueira",
    "alex.miroshnychenko",
    "andrew.siemen",
    "eduardo.casarero",
    "evgeniy.patlan",
    "santiago.ruiz",
    "surabhi.bhat",
    "talha.rizwan",
    "vadim.yalovets",
  ]
}

# Rendered in the root so the ARN uses this account's caller-identity, not
# the eu-west-1 alias's. Secret lives in us-east-1 with the rest of alloy-gateway.
data "aws_iam_policy_document" "ps3_alloy_bearer_read" {
  statement {
    sid       = "AlloyGatewayBearerRead"
    effect    = "Allow"
    actions   = ["secretsmanager:GetSecretValue"]
    resources = ["arn:aws:secretsmanager:us-east-1:${data.aws_caller_identity.current.account_id}:secret:percona-ci-platform/alloy-gateway/bearer-*"]
  }
}

# Consumed by origins.tf's legacy Route53 record. Retire with origins.tf
# once the K8s reconciler is the sole source of truth.
data "aws_instances" "ps3_master" {
  provider             = aws.eu-west-1
  instance_state_names = ["running"]
  instance_tags = {
    "iit-billing-tag" = "jenkins-ps3"
  }
}

# Cross-region VPC peering EKS (us-east-1) <-> ps3 (eu-west-1).
# Folded in from the old peering-ps3.tf; state-mv keeps them in place.

resource "aws_vpc_peering_connection" "ps3" {
  vpc_id      = module.vpc.vpc_id
  peer_vpc_id = module.ps3.vpc_id
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
  destination_cidr_block    = module.ps3.vpc_cidr
  vpc_peering_connection_id = aws_vpc_peering_connection.ps3.id
}

resource "aws_route" "ps3_to_eks" {
  provider                  = aws.eu-west-1
  for_each                  = toset(module.ps3.private_route_table_ids)
  route_table_id            = each.value
  destination_cidr_block    = module.vpc.vpc_cidr_block
  vpc_peering_connection_id = aws_vpc_peering_connection.ps3.id
}

# Imports bring live CF-created resources into module.ps3 state. Removed
# after the first successful apply.

# Networking
import {
  to = module.ps3.aws_vpc.this
  id = "vpc-09e50c80b81ed520c"
}
import {
  to = module.ps3.aws_internet_gateway.this
  id = "igw-0c10b6fcc5f8b8e6e"
}
import {
  to = module.ps3.aws_subnet.this["b"]
  id = "subnet-0d1838e617a72264c"
}
import {
  to = module.ps3.aws_subnet.this["c"]
  id = "subnet-044e425b6978ca1d7"
}
import {
  to = module.ps3.aws_route_table.this
  id = "rtb-05839129e590eee16"
}
import {
  to = module.ps3.aws_route.internet
  id = "rtb-05839129e590eee16_0.0.0.0/0"
}
import {
  to = module.ps3.aws_route_table_association.this["b"]
  id = "subnet-0d1838e617a72264c/rtb-05839129e590eee16"
}
import {
  to = module.ps3.aws_route_table_association.this["c"]
  id = "subnet-044e425b6978ca1d7/rtb-05839129e590eee16"
}
import {
  to = module.ps3.aws_vpc_endpoint.s3
  id = "vpce-052136435278ae725"
}

# Security groups
import {
  to = module.ps3.aws_security_group.ssh
  id = "sg-04634ac3fed10d162"
}
import {
  to = module.ps3.aws_security_group.http
  id = "sg-0e9e17fc851a0a7e3"
}

# IAM — roles, role-policies, instance profiles
import {
  to = module.ps3.aws_iam_role.master
  id = "jenkins-ps3-master"
}
import {
  to = module.ps3.aws_iam_role_policy.master_start_instances
  id = "jenkins-ps3-master:StartInstances"
}
import {
  to = module.ps3.aws_iam_role_policy.master_pass_role
  id = "jenkins-ps3-master:PassRole"
}
import {
  to = module.ps3.aws_iam_role_policy.master_user_data_needs
  id = "jenkins-ps3-master:UserDataNeeds"
}
import {
  to = module.ps3.aws_iam_role_policy.master_read_termination_sqs[0]
  id = "jenkins-ps3-master:ReadTerminationSQS"
}
import {
  to = module.ps3.aws_iam_role_policy.master_extras["AlloyGatewayBearerRead"]
  id = "jenkins-ps3-master:AlloyGatewayBearerRead"
}
import {
  to = module.ps3.aws_iam_instance_profile.master
  id = "jenkins-ps3"
}
import {
  to = module.ps3.aws_iam_role.worker
  id = "jenkins-ps3-worker"
}
import {
  to = module.ps3.aws_iam_role_policy.worker
  id = "jenkins-ps3-worker:jenkins-ps3-worker"
}
import {
  to = module.ps3.aws_iam_instance_profile.worker
  id = "jenkins-ps3-worker"
}
import {
  to = module.ps3.aws_iam_role.spot_fleet
  id = "jenkins-ps3-SpotFleet"
}

# Managed-policy attachments (replace deprecated managed_policy_arns).
import {
  to = module.ps3.aws_iam_role_policy_attachment.master_managed["arn:aws:iam::aws:policy/AmazonSSMManagedInstanceCore"]
  id = "jenkins-ps3-master/arn:aws:iam::aws:policy/AmazonSSMManagedInstanceCore"
}
import {
  to = module.ps3.aws_iam_role_policy_attachment.master_managed["arn:aws:iam::aws:policy/CloudWatchAgentServerPolicy"]
  id = "jenkins-ps3-master/arn:aws:iam::aws:policy/CloudWatchAgentServerPolicy"
}
import {
  to = module.ps3.aws_iam_role_policy_attachment.master_managed["arn:aws:iam::${data.aws_caller_identity.current.account_id}:policy/ec2.DescribeSpotPriceHistory"]
  id = "jenkins-ps3-master/arn:aws:iam::${data.aws_caller_identity.current.account_id}:policy/ec2.DescribeSpotPriceHistory"
}
import {
  to = module.ps3.aws_iam_role_policy_attachment.spot_fleet_tagging
  id = "jenkins-ps3-SpotFleet/arn:aws:iam::aws:policy/service-role/AmazonEC2SpotFleetTaggingRole"
}

# Persistent data + compute
import {
  to = module.ps3.aws_ebs_volume.data
  id = "vol-06ce3f52efb4d163f"
}
import {
  to = module.ps3.aws_launch_template.master
  id = "lt-07d3b5037b0b3bb3d"
}
import {
  to = module.ps3.aws_spot_fleet_request.master
  id = "sfr-02331b67-2e5d-4239-ab66-331f016b0235"
}

# Termination queue trio
import {
  to = module.ps3.aws_sqs_queue.termination[0]
  id = "https://sqs.eu-west-1.amazonaws.com/119175775298/jenkins-ps3-termination"
}
import {
  to = module.ps3.aws_sqs_queue_policy.termination[0]
  id = "https://sqs.eu-west-1.amazonaws.com/119175775298/jenkins-ps3-termination"
}
import {
  to = module.ps3.aws_cloudwatch_event_rule.termination[0]
  id = "default/jenkins-ps3-TerminationRule1-ORED93LDNS23"
}
import {
  to = module.ps3.aws_cloudwatch_event_target.termination[0]
  id = "default/jenkins-ps3-TerminationRule1-ORED93LDNS23/jenkins-ps3-termination"
}

# aws_eip.master is intentionally not imported (see file header).
