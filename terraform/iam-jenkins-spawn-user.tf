# Owner: platform

# jenkins-spawn-user, the CI IAM user whose access key Jenkins jobs use to
# launch EC2 instances for AWS_STASH and package-testing work.
#
# Imported, not created. The access key stays out of Terraform: a managed
# aws_iam_access_key cannot carry an existing secret, so declaring one would
# mint a second key and a replacement would break the stored credential.
#
# jenkins-artifactory and ec2_describe_instance_status are hand-made and shared
# with other principals, so this file owns the attachments, not the documents.
# Re-attaching is idempotent in IAM, so they need no import block. The molecule
# read grant is attached in iam-molecule.tf.
#
# iam:PassRole targets one worker role rather than a wildcard, so a leaked key
# cannot pass an arbitrary role to an instance.

import {
  to = aws_iam_user.jenkins_spawn
  id = "jenkins-spawn-user"
}

resource "aws_iam_user" "jenkins_spawn" {
  name = "jenkins-spawn-user"
  path = "/"

  # Own billing line, overriding the provider-wide default value.
  tags = {
    "iit-billing-tag" = "jenkins-spawn-user"
  }
}

data "aws_iam_policy_document" "jenkins_spawn_ec2_access" {
  statement {
    effect = "Allow"
    actions = [
      "ec2:ModifySpotFleetRequest",
      "ec2:DescribeSpotFleetRequests",
      "ec2:DescribeSpotFleetInstances",
      "ec2:DescribeSpotInstanceRequests",
      "ec2:CancelSpotInstanceRequests",
      "ec2:GetConsoleOutput",
      "ec2:RequestSpotInstances",
      "ec2:RunInstances",
      "ec2:StartInstances",
      "ec2:StopInstances",
      "ec2:TerminateInstances",
      "ec2:CreateTags",
      "ec2:CreateSecurityGroup",
      "ec2:DeleteTags",
      "ec2:DescribeInstances",
      "ec2:DescribeKeyPairs",
      "ec2:DescribeRegions",
      "ec2:DescribeImages",
      "ec2:DescribeAvailabilityZones",
      "ec2:DescribeSecurityGroups",
      "ec2:DescribeSubnets",
      "ec2:AuthorizeSecurityGroupEgress",
      "ec2:AuthorizeSecurityGroupIngress",
      "ec2:RevokeSecurityGroupEgress",
      "ec2:RevokeSecurityGroupIngress",
    ]
    resources = ["*"]
  }
}

import {
  to = aws_iam_user_policy.jenkins_spawn_ec2_access
  id = "jenkins-spawn-user:EC2Access"
}

resource "aws_iam_user_policy" "jenkins_spawn_ec2_access" {
  name   = "EC2Access"
  user   = aws_iam_user.jenkins_spawn.name
  policy = data.aws_iam_policy_document.jenkins_spawn_ec2_access.json
}

data "aws_iam_policy_document" "jenkins_spawn_passrole" {
  statement {
    effect    = "Allow"
    actions   = ["iam:PassRole"]
    resources = ["arn:aws:iam::${data.aws_caller_identity.current.account_id}:role/jenkins-psmdb-slave"]
  }
}

import {
  to = aws_iam_user_policy.jenkins_spawn_passrole
  id = "jenkins-spawn-user:allow_passrole_ec2"
}

resource "aws_iam_user_policy" "jenkins_spawn_passrole" {
  name   = "allow_passrole_ec2"
  user   = aws_iam_user.jenkins_spawn.name
  policy = data.aws_iam_policy_document.jenkins_spawn_passrole.json
}

resource "aws_iam_user_policy_attachment" "jenkins_spawn_artifactory" {
  user       = aws_iam_user.jenkins_spawn.name
  policy_arn = "arn:aws:iam::${data.aws_caller_identity.current.account_id}:policy/jenkins-artifactory"
}

resource "aws_iam_user_policy_attachment" "jenkins_spawn_ec2_describe_instance_status" {
  user       = aws_iam_user.jenkins_spawn.name
  policy_arn = "arn:aws:iam::${data.aws_caller_identity.current.account_id}:policy/ec2_describe_instance_status"
}
