# TEMPORARY config-driven import blocks for the pxb keep-VPC migration.
# Adopts the live (legacy-Terraform-managed) pxb resources into the
# jenkins-master module without recreating them. The old standalone state
# (terraform-state-storage-pxb) is `state rm`'d AFTER these imports succeed, so
# the carry-forward resources never have two destroyers.
#
# Remove this file once the first apply completes (the resources stay in state).
#
# NOT imported (created fresh or handled separately):
#   - aws_iam_instance_profile.master (live name jenkins-pxb-master; module uses
#     jenkins-pxb -> recreated fresh, old one hand-deleted in cleanup)
#   - aws_cloudwatch_event_rule/target.termination (live name jenkins-pxb-aws-stop;
#     module name differs -> recreated; old hand-deleted)
#   - aws_eip / aws_route53_record (EIP-less + external-dns; old A->EIP destroyed
#     via the legacy state)
#   - subnet "a" (10.179.0.0/24) + its assoc (module has no "a" -> dropped)
#   - aws_vpc_endpoint.s3, launch template, instance, S3 init bucket, arm fleet,
#     peering trio (all net-new)

import {
  to = module.pxb.aws_vpc.this
  id = "vpc-0a071372c786e7276"
}

import {
  to = module.pxb.aws_internet_gateway.this
  id = "igw-027164f313e7a7904"
}

import {
  to = module.pxb.aws_subnet.this["b"]
  id = "subnet-011f09cf273aeef73"
}

import {
  to = module.pxb.aws_subnet.this["c"]
  id = "subnet-00b0d1d8bd8af5c07"
}

import {
  to = module.pxb.aws_route_table.this
  id = "rtb-0cf441e3ecf5d9040"
}

import {
  to = module.pxb.aws_route.internet
  id = "rtb-0cf441e3ecf5d9040_0.0.0.0/0"
}

import {
  to = module.pxb.aws_route_table_association.this["b"]
  id = "subnet-011f09cf273aeef73/rtb-0cf441e3ecf5d9040"
}

import {
  to = module.pxb.aws_route_table_association.this["c"]
  id = "subnet-00b0d1d8bd8af5c07/rtb-0cf441e3ecf5d9040"
}

# NOTE: the SSH/HTTP security groups are deliberately NOT imported. The live
# ones are named jenkins-pxb-SSH / jenkins-pxb-HTTP, while the module names its
# SGs "SSH" / "HTTP"; SG name is immutable, so importing would force a
# delete+create (churn + DependencyViolation against the running spot instance).
# Instead the module creates fresh SSH/HTTP SGs in the kept VPC (no name
# collision with the jenkins-pxb-* ones), the new instance uses them, and the
# old SGs are hand-deleted in cleanup once the spot instance is gone.

import {
  to = module.pxb.aws_ebs_volume.data
  id = "vol-08cacafbce3ce7fdd"
}

import {
  to = module.pxb.aws_iam_role.master
  id = "jenkins-pxb-master"
}

import {
  to = module.pxb.aws_iam_role_policy_attachment.master_managed["arn:aws:iam::aws:policy/AmazonSSMManagedInstanceCore"]
  id = "jenkins-pxb-master/arn:aws:iam::aws:policy/AmazonSSMManagedInstanceCore"
}

import {
  to = module.pxb.aws_iam_role_policy_attachment.master_managed["arn:aws:iam::aws:policy/CloudWatchAgentServerPolicy"]
  id = "jenkins-pxb-master/arn:aws:iam::aws:policy/CloudWatchAgentServerPolicy"
}

import {
  to = module.pxb.aws_iam_role.worker
  id = "jenkins-pxb-worker"
}

import {
  to = module.pxb.aws_iam_instance_profile.worker
  id = "jenkins-pxb-worker"
}

import {
  to = module.pxb.aws_sqs_queue.termination[0]
  id = "https://sqs.us-west-2.amazonaws.com/119175775298/jenkins-pxb-termination"
}

import {
  to = module.pxb.aws_sqs_queue_policy.termination[0]
  id = "https://sqs.us-west-2.amazonaws.com/119175775298/jenkins-pxb-termination"
}
