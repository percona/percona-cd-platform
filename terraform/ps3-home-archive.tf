# Owner: mysql
# Durable archive of the decommissioned EC2 ps3 master's JENKINS_HOME:
# s3://jenkins-ps3-home-archive plus a dedicated CMK
# (alias/jenkins-ps3-home-archive), both in eu-west-1. Preservation leg 3 of 3
# alongside the retained EBS volume and its snapshot; see
# docs/runbooks/decommission-ps3-ec2-master.md. Born as a local-state one-off
# root during the decommission, adopted here via import blocks (same pattern
# as molecule-tests.tf); the blocks are idempotent no-ops once state holds the
# addresses and stay in-tree as the record of the real-world IDs.
#
# Objects self-expire via the expire-90d lifecycle rule (the sync completed
# 2026-06-07, so the data is gone around 2026-09-05). The bucket and CMK stay
# managed until a deliberate teardown PR lifts prevent_destroy and removes
# this file: the archive holds secrets/master.key and credentials.xml, so
# teardown is a reviewed decision, never a side effect. The master role's
# write-only grant was removed after the sync completed; nothing writes here.

import {
  to = aws_kms_key.ps3_home_archive
  id = "d509ef7f-e1c1-4614-a95e-4affb96772a0"
}

# Dedicated CMK for the archive bucket (separate blast radius from the
# cluster / backup / LGTM keys). Default-deny key policy: only the account
# root retains kms:*, which delegates day-to-day use to IAM (same split as
# lgtm-storage.tf).
resource "aws_kms_key" "ps3_home_archive" {
  provider                = aws.eu-west-1
  description             = "ps3 JENKINS_HOME decommission archive encryption (temporary)"
  deletion_window_in_days = 7
  enable_key_rotation     = true
  policy                  = data.aws_iam_policy_document.ps3_home_archive_kms.json

  tags = {
    "iit-billing-tag" = "jenkins-ps3"
    team              = "mysql"
    purpose           = "ps3-decommission-archive"
  }

  lifecycle {
    prevent_destroy = true
  }
}

import {
  to = aws_kms_alias.ps3_home_archive
  id = "alias/jenkins-ps3-home-archive"
}

resource "aws_kms_alias" "ps3_home_archive" {
  provider      = aws.eu-west-1
  name          = "alias/jenkins-ps3-home-archive"
  target_key_id = aws_kms_key.ps3_home_archive.key_id
}

data "aws_iam_policy_document" "ps3_home_archive_kms" {
  statement {
    sid       = "EnableRootAccess"
    actions   = ["kms:*"]
    resources = ["*"]
    principals {
      type        = "AWS"
      identifiers = ["arn:aws:iam::${data.aws_caller_identity.current.account_id}:root"]
    }
  }
}

import {
  to = aws_s3_bucket.ps3_home_archive
  id = "jenkins-ps3-home-archive"
}

resource "aws_s3_bucket" "ps3_home_archive" {
  provider = aws.eu-west-1
  bucket   = "jenkins-ps3-home-archive"

  tags = {
    Name              = "jenkins-ps3-home-archive"
    "iit-billing-tag" = "jenkins-ps3"
    team              = "mysql"
    purpose           = "ps3-decommission-archive"
  }

  lifecycle {
    prevent_destroy = true
  }
}

import {
  to = aws_s3_bucket_ownership_controls.ps3_home_archive
  id = "jenkins-ps3-home-archive"
}

# BucketOwnerEnforced: ACLs off, IAM is the only access path.
resource "aws_s3_bucket_ownership_controls" "ps3_home_archive" {
  provider = aws.eu-west-1
  bucket   = aws_s3_bucket.ps3_home_archive.id
  rule {
    object_ownership = "BucketOwnerEnforced"
  }
}

import {
  to = aws_s3_bucket_versioning.ps3_home_archive
  id = "jenkins-ps3-home-archive"
}

resource "aws_s3_bucket_versioning" "ps3_home_archive" {
  provider = aws.eu-west-1
  bucket   = aws_s3_bucket.ps3_home_archive.id
  versioning_configuration {
    status = "Enabled"
  }
}

import {
  to = aws_s3_bucket_public_access_block.ps3_home_archive
  id = "jenkins-ps3-home-archive"
}

resource "aws_s3_bucket_public_access_block" "ps3_home_archive" {
  provider                = aws.eu-west-1
  bucket                  = aws_s3_bucket.ps3_home_archive.id
  block_public_acls       = true
  block_public_policy     = true
  ignore_public_acls      = true
  restrict_public_buckets = true
}

import {
  to = aws_s3_bucket_server_side_encryption_configuration.ps3_home_archive
  id = "jenkins-ps3-home-archive"
}

resource "aws_s3_bucket_server_side_encryption_configuration" "ps3_home_archive" {
  provider = aws.eu-west-1
  bucket   = aws_s3_bucket.ps3_home_archive.id
  rule {
    apply_server_side_encryption_by_default {
      sse_algorithm     = "aws:kms"
      kms_master_key_id = aws_kms_key.ps3_home_archive.arn
    }
    bucket_key_enabled = true
  }
}

import {
  to = aws_s3_bucket_lifecycle_configuration.ps3_home_archive
  id = "jenkins-ps3-home-archive"
}

# Temporary archive: expire everything after 90 days, drop noncurrent versions
# after a week, and abort dangling multipart uploads (the home sync was
# multipart).
resource "aws_s3_bucket_lifecycle_configuration" "ps3_home_archive" {
  provider = aws.eu-west-1
  bucket   = aws_s3_bucket.ps3_home_archive.id
  rule {
    id     = "expire-90d"
    status = "Enabled"
    filter {
      prefix = ""
    }
    expiration {
      days = 90
    }
    noncurrent_version_expiration {
      noncurrent_days = 7
    }
    abort_incomplete_multipart_upload {
      days_after_initiation = 7
    }
  }
}

# Enforce TLS in transit and KMS-only PutObject (the home holds secrets/, so
# an object can never land unencrypted even if a future caller omits the
# flag).
data "aws_iam_policy_document" "ps3_home_archive_bucket" {
  statement {
    sid       = "DenyInsecureTransport"
    effect    = "Deny"
    actions   = ["s3:*"]
    resources = [aws_s3_bucket.ps3_home_archive.arn, "${aws_s3_bucket.ps3_home_archive.arn}/*"]
    principals {
      type        = "*"
      identifiers = ["*"]
    }
    condition {
      test     = "Bool"
      variable = "aws:SecureTransport"
      values   = ["false"]
    }
  }
  statement {
    sid       = "DenyUnencryptedPuts"
    effect    = "Deny"
    actions   = ["s3:PutObject"]
    resources = ["${aws_s3_bucket.ps3_home_archive.arn}/*"]
    principals {
      type        = "*"
      identifiers = ["*"]
    }
    condition {
      test     = "StringNotEquals"
      variable = "s3:x-amz-server-side-encryption"
      values   = ["aws:kms"]
    }
  }
}

import {
  to = aws_s3_bucket_policy.ps3_home_archive
  id = "jenkins-ps3-home-archive"
}

resource "aws_s3_bucket_policy" "ps3_home_archive" {
  provider = aws.eu-west-1
  bucket   = aws_s3_bucket.ps3_home_archive.id
  policy   = data.aws_iam_policy_document.ps3_home_archive_bucket.json
}
