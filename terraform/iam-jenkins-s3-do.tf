# Owner: platform

# jenkins-s3-do, the CI IAM user whose access key the QA package-testing and
# artifactory jobs use.
#
# Imported, not created. Its access keys stay out of Terraform: a managed
# aws_iam_access_key cannot carry an existing secret, so declaring one would
# mint a new key and a replacement would break the stored credential.
#
# The managed policies are either AWS-managed or hand-made and shared with
# other principals, so this file owns the attachments, not the documents.
# Re-attaching is idempotent in IAM, so they need no import block. The molecule
# read grant is attached in iam-molecule.tf and is deliberately absent here, so
# each attachment keeps exactly one owner.
#
# The grants are wider than a least-privilege design would pick: account-wide S3
# through AmazonS3FullAccess, and wildcard resources on the EC2 statement.
# Narrowing them changes permissions the QA jobs depend on, so that is its own
# change rather than a side effect of adopting the user.

import {
  to = aws_iam_user.jenkins_s3_do
  id = "jenkins-s3-do"
}

resource "aws_iam_user" "jenkins_s3_do" {
  name = "jenkins-s3-do"
  path = "/"

  # Own billing line, overriding the provider-wide default value.
  tags = {
    "iit-billing-tag" = "jenkins-s3-do"
  }
}

data "aws_iam_policy_document" "jenkins_s3_do_ec2_access" {
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
  to = aws_iam_user_policy.jenkins_s3_do_ec2_access
  id = "jenkins-s3-do:EC2Access"
}

resource "aws_iam_user_policy" "jenkins_s3_do_ec2_access" {
  name   = "EC2Access"
  user   = aws_iam_user.jenkins_s3_do.name
  policy = data.aws_iam_policy_document.jenkins_s3_do_ec2_access.json
}

data "aws_iam_policy_document" "jenkins_s3_do_passrole" {
  statement {
    effect    = "Allow"
    actions   = ["iam:PassRole"]
    resources = ["arn:aws:iam::${data.aws_caller_identity.current.account_id}:role/jenkins-psmdb-slave"]
  }
}

import {
  to = aws_iam_user_policy.jenkins_s3_do_passrole
  id = "jenkins-s3-do:allow_passrole_ec2"
}

resource "aws_iam_user_policy" "jenkins_s3_do_passrole" {
  name   = "allow_passrole_ec2"
  user   = aws_iam_user.jenkins_s3_do.name
  policy = data.aws_iam_policy_document.jenkins_s3_do_passrole.json
}

data "aws_iam_policy_document" "jenkins_s3_do_artifactory" {
  statement {
    sid    = "VisualEditor0"
    effect = "Allow"
    actions = [
      "s3:GetAccessPoint",
      "s3:PutAccountPublicAccessBlock",
      "s3:GetAccountPublicAccessBlock",
      "s3:ListAllMyBuckets",
      "s3:ListAccessPoints",
      "s3:ListJobs",
      "s3:CreateJob",
      "s3:HeadBucket",
    ]
    resources = ["*"]
  }

  statement {
    sid     = "VisualEditor1"
    effect  = "Allow"
    actions = ["s3:*"]
    resources = [
      "arn:aws:s3:*:*:accesspoint/*",
      "arn:aws:s3:::*/*",
      "arn:aws:s3:*:*:job/*",
      "arn:aws:s3:::percona-jenkins-artifactory",
    ]
  }
}

import {
  to = aws_iam_user_policy.jenkins_s3_do_artifactory
  id = "jenkins-s3-do:jenkins-artifactory"
}

resource "aws_iam_user_policy" "jenkins_s3_do_artifactory" {
  name   = "jenkins-artifactory"
  user   = aws_iam_user.jenkins_s3_do.name
  policy = data.aws_iam_policy_document.jenkins_s3_do_artifactory.json
}

data "aws_iam_policy_document" "jenkins_s3_do_kms_testing" {
  statement {
    sid    = "AllowKMSTestingKey"
    effect = "Allow"
    actions = [
      "kms:Encrypt",
      "kms:Decrypt",
      "kms:ReEncrypt*",
      "kms:GenerateDataKey*",
      "kms:DescribeKey",
    ]
    resources = ["arn:aws:kms:us-east-1:${data.aws_caller_identity.current.account_id}:key/a7d4a714-7dad-4ae1-a0fa-0a647c86dcde"]
  }
}

import {
  to = aws_iam_user_policy.jenkins_s3_do_kms_testing
  id = "jenkins-s3-do:KMSTestingKeyAccess"
}

resource "aws_iam_user_policy" "jenkins_s3_do_kms_testing" {
  name   = "KMSTestingKeyAccess"
  user   = aws_iam_user.jenkins_s3_do.name
  policy = data.aws_iam_policy_document.jenkins_s3_do_kms_testing.json
}

resource "aws_iam_user_policy_attachment" "jenkins_s3_do_s3_full" {
  user       = aws_iam_user.jenkins_s3_do.name
  policy_arn = "arn:aws:iam::aws:policy/AmazonS3FullAccess"
}

resource "aws_iam_user_policy_attachment" "jenkins_s3_do_ecr_public_read" {
  user       = aws_iam_user.jenkins_s3_do.name
  policy_arn = "arn:aws:iam::aws:policy/AmazonElasticContainerRegistryPublicReadOnly"
}

resource "aws_iam_user_policy_attachment" "jenkins_s3_do_ec2_describe_instance_status" {
  user       = aws_iam_user.jenkins_s3_do.name
  policy_arn = "arn:aws:iam::${data.aws_caller_identity.current.account_id}:policy/ec2_describe_instance_status"
}
