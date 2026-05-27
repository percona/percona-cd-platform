# Terraform import blocks for the ps80.cd.percona.com CFN-to-TF migration.
#
# Each block tells Terraform to adopt the existing AWS resource (created by
# CloudFormation stack `jenkins-ps80` in us-west-2) into the `module.ps80`
# state instead of creating a new one. Physical IDs were resolved from
# `aws cloudformation describe-stack-resources --stack-name jenkins-ps80`
# plus a few targeted EC2 / Route53 lookups (EIP allocation ID, hosted-zone
# ID, etc).
#
# Workflow:
#   1. tofu validate                  # syntax check (no AWS calls)
#   2. tofu plan                      # should show "X to import, 0 to add"
#                                       for the resources below; remaining
#                                       diffs are the SpotFleet -> on-demand
#                                       swap + LaunchTemplate replacement.
#   3. tofu apply                     # adopts the resources into state.
#   4. Delete this file once import   # imports are one-shot; leaving the
#      is complete (or after the        blocks in is harmless after apply
#      next successful apply).          but adds noise.
#
# CFN stack `jenkins-ps80` itself stays around until the TF apply succeeds;
# delete it (with DeletionPolicy: Retain on all 22 resources first, same as
# ps3 on 2026-05-19) only after `tofu plan` is clean.

# ---------------------------------------------------------------------------
# VPC + networking
# ---------------------------------------------------------------------------

import {
  id = "vpc-09d0442cdd2213754"
  to = module.ps80.aws_vpc.this
}

import {
  id = "igw-06e8628ac3ec22dcd"
  to = module.ps80.aws_internet_gateway.this
}

import {
  id = "subnet-028f5265edd1581c3"
  to = module.ps80.aws_subnet.this["b"]
}

import {
  id = "subnet-0fe430aa745b0116f"
  to = module.ps80.aws_subnet.this["c"]
}

import {
  id = "rtb-080aedbc7c6697452"
  to = module.ps80.aws_route_table.this
}

import {
  id = "rtb-080aedbc7c6697452_0.0.0.0/0"
  to = module.ps80.aws_route.internet
}

import {
  id = "subnet-028f5265edd1581c3/rtb-080aedbc7c6697452"
  to = module.ps80.aws_route_table_association.this["b"]
}

import {
  id = "subnet-0fe430aa745b0116f/rtb-080aedbc7c6697452"
  to = module.ps80.aws_route_table_association.this["c"]
}

import {
  id = "vpce-0cb066918709aba9d"
  to = module.ps80.aws_vpc_endpoint.s3
}

# ---------------------------------------------------------------------------
# Security groups
# ---------------------------------------------------------------------------

import {
  id = "sg-005bb54274483f042"
  to = module.ps80.aws_security_group.ssh
}

import {
  id = "sg-00c10e9d9761c0bed"
  to = module.ps80.aws_security_group.http
}

# ---------------------------------------------------------------------------
# IAM (worker)
# ---------------------------------------------------------------------------

import {
  id = "jenkins-ps80-worker"
  to = module.ps80.aws_iam_role.worker
}

import {
  id = "jenkins-ps80-worker"
  to = module.ps80.aws_iam_instance_profile.worker
}

# ---------------------------------------------------------------------------
# IAM (master)
# ---------------------------------------------------------------------------

import {
  id = "jenkins-ps80-master"
  to = module.ps80.aws_iam_role.master
}

import {
  id = "jenkins-ps80"
  to = module.ps80.aws_iam_instance_profile.master
}

# ---------------------------------------------------------------------------
# EIP + Route53
# ---------------------------------------------------------------------------

# Resolved via `ec2 describe-addresses --public-ips 44.235.60.227`.
import {
  id = "eipalloc-0194d5f28681d3358"
  to = module.ps80.aws_eip.master[0]
}

# Route53 import ID format: <zone_id>_<name>_<type>.
# Zone `cd.percona.com` => Z1H0AFAU7N8IMC.
import {
  id = "Z1H0AFAU7N8IMC_ps80.cd.percona.com_A"
  to = module.ps80.aws_route53_record.master[0]
}

# ---------------------------------------------------------------------------
# Data volume
# ---------------------------------------------------------------------------

import {
  id = "vol-09c0b26ce75129c24"
  to = module.ps80.aws_ebs_volume.data
}

# ---------------------------------------------------------------------------
# Spot-interruption SQS + EventBridge wiring
# ---------------------------------------------------------------------------

import {
  id = "https://sqs.us-west-2.amazonaws.com/119175775298/jenkins-ps80-termination"
  to = module.ps80.aws_sqs_queue.termination[0]
}

import {
  id = "https://sqs.us-west-2.amazonaws.com/119175775298/jenkins-ps80-termination"
  to = module.ps80.aws_sqs_queue_policy.termination[0]
}

import {
  id = "jenkins-ps80-TerminationRule1-ya9MCSScqXVs"
  to = module.ps80.aws_cloudwatch_event_rule.termination[0]
}

# EventBridge target import ID format: <rule-name>/<target-id>.
# The module sets target_id = aws_sqs_queue.termination[0].name => "jenkins-ps80-termination".
import {
  id = "jenkins-ps80-TerminationRule1-ya9MCSScqXVs/jenkins-ps80-termination"
  to = module.ps80.aws_cloudwatch_event_target.termination[0]
}
