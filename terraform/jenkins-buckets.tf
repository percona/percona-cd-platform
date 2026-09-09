# Owner: platform

# The long-lived S3 buckets the Jenkins fleet reads and writes: per-product
# build caches, the cross-fleet artifact store, the job XML backup target,
# the PXB backup-test bucket and the PMM Server OVA store.
#
# Imported, not created. Every bucket predates this repo and holds live data,
# so each resource below mirrors the bucket's current configuration exactly:
# same lifecycle rules, logging targets, access blocks, ACLs and policies.
# Buckets that carry no public-access block, no ownership control or a private
# canned ACL declare none, so the import lands with no configuration change.
# A bucket's owning team is asserted through the `team` tag and its billing
# line through `iit-billing-tag`, overriding the provider-wide defaults.
#
# Worker and controller IAM grants on these buckets live with their masters
# (cache_bucket_name in master-*.tf) and in iam-jenkins-s3-do.tf.
#
# Regions follow the masters that use each bucket: PS and PMM caches in
# us-east-2, PXB and PXC in us-west-2, the release repo cache in eu-west-1,
# and the account-wide buckets (artifactory, job backups, OVA store) in
# us-east-1 under the default provider.

locals {
  s3_access_logs_bucket = "s3-access-logs-${data.aws_caller_identity.current.account_id}"
}

# ---------------------------------------------------------------------------
# ps-build-cache (us-east-2): ccache and build-artifact cache shared by the
# PS family masters (ps3, ps57, ps80). Versioning is suspended. Objects expire
# at 30 days, or sooner when tagged RetentionDays 7, 14 or 21. The 60 and 120
# day tag rules are imported as they are but never win, because S3 applies the
# shortest overlapping expiration and the 30 day rule matches every object.
# ---------------------------------------------------------------------------

import {
  provider = aws.us-east-2
  to       = aws_s3_bucket.ps_build_cache
  id       = "ps-build-cache"
}

resource "aws_s3_bucket" "ps_build_cache" {
  provider = aws.us-east-2
  bucket   = "ps-build-cache"

  tags = {
    "iit-billing-tag" = "jenkins-ps"
    team              = "mysql"
  }

  lifecycle {
    prevent_destroy = true
  }
}

import {
  provider = aws.us-east-2
  to       = aws_s3_bucket_server_side_encryption_configuration.ps_build_cache
  id       = "ps-build-cache"
}

resource "aws_s3_bucket_server_side_encryption_configuration" "ps_build_cache" {
  provider = aws.us-east-2
  bucket   = aws_s3_bucket.ps_build_cache.id

  rule {
    apply_server_side_encryption_by_default {
      sse_algorithm = "AES256"
    }
    bucket_key_enabled       = false
    blocked_encryption_types = ["NONE"]
  }
}

import {
  provider = aws.us-east-2
  to       = aws_s3_bucket_versioning.ps_build_cache
  id       = "ps-build-cache"
}

resource "aws_s3_bucket_versioning" "ps_build_cache" {
  provider = aws.us-east-2
  bucket   = aws_s3_bucket.ps_build_cache.id

  versioning_configuration {
    status = "Suspended"
  }
}

import {
  provider = aws.us-east-2
  to       = aws_s3_bucket_public_access_block.ps_build_cache
  id       = "ps-build-cache"
}

# Left fully open on purpose: build scripts upload cache objects with
# public-read object ACLs that consumers fetch anonymously.
#trivy:ignore:AVD-AWS-0086
#trivy:ignore:AVD-AWS-0087
#trivy:ignore:AVD-AWS-0091
#trivy:ignore:AVD-AWS-0093
resource "aws_s3_bucket_public_access_block" "ps_build_cache" {
  provider = aws.us-east-2
  bucket   = aws_s3_bucket.ps_build_cache.id

  block_public_acls       = false
  block_public_policy     = false
  ignore_public_acls      = false
  restrict_public_buckets = false
}

import {
  provider = aws.us-east-2
  to       = aws_s3_bucket_lifecycle_configuration.ps_build_cache
  id       = "ps-build-cache"
}

resource "aws_s3_bucket_lifecycle_configuration" "ps_build_cache" {
  provider = aws.us-east-2
  bucket   = aws_s3_bucket.ps_build_cache.id

  transition_default_minimum_object_size = "all_storage_classes_128K"

  rule {
    id     = "30 Days Cleanup"
    status = "Enabled"

    filter {
      prefix = ""
    }

    expiration {
      days = 30
    }

    noncurrent_version_expiration {
      noncurrent_days = 1
    }

    abort_incomplete_multipart_upload {
      days_after_initiation = 1
    }
  }

  dynamic "rule" {
    for_each = [7, 14, 21, 30, 60, 120]

    content {
      id     = "Delete${rule.value}DayRetentionObjects"
      status = "Enabled"

      filter {
        tag {
          key   = "RetentionDays"
          value = tostring(rule.value)
        }
      }

      expiration {
        days = rule.value
      }

      noncurrent_version_expiration {
        noncurrent_days = 1
      }
    }
  }
}

# ---------------------------------------------------------------------------
# pmm-build-cache (us-east-2): PR-build pmm-client tarballs. The bucket ACL
# allows anonymous listing, the bucket policy allows anonymous object reads,
# objects expire after 30 days.
# ---------------------------------------------------------------------------

import {
  provider = aws.us-east-2
  to       = aws_s3_bucket.pmm_build_cache
  id       = "pmm-build-cache"
}

# No public-access block: the public-read bucket ACL allows anonymous listing and the bucket policy below grants anonymous object reads, both by design.
#trivy:ignore:AVD-AWS-0086
#trivy:ignore:AVD-AWS-0087
#trivy:ignore:AVD-AWS-0091
#trivy:ignore:AVD-AWS-0093
resource "aws_s3_bucket" "pmm_build_cache" {
  provider = aws.us-east-2
  bucket   = "pmm-build-cache"

  tags = {
    "iit-billing-tag" = "pmm-feature-builds"
    team              = "pmm"
    PerconaApproved   = "HD-26384"
  }

  lifecycle {
    prevent_destroy = true
  }
}

import {
  provider = aws.us-east-2
  to       = aws_s3_bucket_server_side_encryption_configuration.pmm_build_cache
  id       = "pmm-build-cache"
}

resource "aws_s3_bucket_server_side_encryption_configuration" "pmm_build_cache" {
  provider = aws.us-east-2
  bucket   = aws_s3_bucket.pmm_build_cache.id

  rule {
    apply_server_side_encryption_by_default {
      sse_algorithm = "AES256"
    }
    bucket_key_enabled       = false
    blocked_encryption_types = ["NONE"]
  }
}

import {
  provider = aws.us-east-2
  to       = aws_s3_bucket_acl.pmm_build_cache
  id       = "pmm-build-cache,public-read"
}

# Canned public-read, the same grant set CloudFormation declared: owner
# FULL_CONTROL plus AllUsers READ (bucket listing only).
#trivy:ignore:AVD-AWS-0092
resource "aws_s3_bucket_acl" "pmm_build_cache" {
  provider = aws.us-east-2
  bucket   = aws_s3_bucket.pmm_build_cache.id
  acl      = "public-read"
}

import {
  provider = aws.us-east-2
  to       = aws_s3_bucket_lifecycle_configuration.pmm_build_cache
  id       = "pmm-build-cache"
}

resource "aws_s3_bucket_lifecycle_configuration" "pmm_build_cache" {
  provider = aws.us-east-2
  bucket   = aws_s3_bucket.pmm_build_cache.id

  transition_default_minimum_object_size = "varies_by_storage_class"

  rule {
    id     = "delete_files_older_than_30_days"
    status = "Enabled"

    expiration {
      days = 30
    }
  }
}

# Bucket ARNs are spelled out so the policy documents resolve at plan time
# instead of waiting on the imported bucket resources.
data "aws_iam_policy_document" "pmm_build_cache" {
  statement {
    sid       = "AllowPublicRead"
    effect    = "Allow"
    actions   = ["s3:GetObject"]
    resources = ["arn:aws:s3:::pmm-build-cache/*"]

    principals {
      type        = "AWS"
      identifiers = ["*"]
    }
  }
}

import {
  provider = aws.us-east-2
  to       = aws_s3_bucket_policy.pmm_build_cache
  id       = "pmm-build-cache"
}

resource "aws_s3_bucket_policy" "pmm_build_cache" {
  provider = aws.us-east-2
  bucket   = aws_s3_bucket.pmm_build_cache.id
  policy   = data.aws_iam_policy_document.pmm_build_cache.json
}

# ---------------------------------------------------------------------------
# pxc-build-cache (us-west-2): PXC build and test artifact cache. Objects are
# uploaded with public-read object ACLs, so the bucket carries no access
# block. Three overlapping expiry rules, the last one a 30 day catch-all.
# ---------------------------------------------------------------------------

import {
  provider = aws.us-west-2
  to       = aws_s3_bucket.pxc_build_cache
  id       = "pxc-build-cache"
}

# No public-access block: build scripts upload objects with public-read ACLs that consumers fetch anonymously.
#trivy:ignore:AVD-AWS-0086
#trivy:ignore:AVD-AWS-0087
#trivy:ignore:AVD-AWS-0091
#trivy:ignore:AVD-AWS-0093
resource "aws_s3_bucket" "pxc_build_cache" {
  provider = aws.us-west-2
  bucket   = "pxc-build-cache"

  tags = {
    "iit-billing-tag" = "jenkins-pxc"
    team              = "pxc"
  }

  lifecycle {
    prevent_destroy = true
  }
}

import {
  provider = aws.us-west-2
  to       = aws_s3_bucket_server_side_encryption_configuration.pxc_build_cache
  id       = "pxc-build-cache"
}

resource "aws_s3_bucket_server_side_encryption_configuration" "pxc_build_cache" {
  provider = aws.us-west-2
  bucket   = aws_s3_bucket.pxc_build_cache.id

  rule {
    apply_server_side_encryption_by_default {
      sse_algorithm = "AES256"
    }
    bucket_key_enabled       = false
    blocked_encryption_types = ["NONE"]
  }
}

import {
  provider = aws.us-west-2
  to       = aws_s3_bucket_lifecycle_configuration.pxc_build_cache
  id       = "pxc-build-cache"
}

resource "aws_s3_bucket_lifecycle_configuration" "pxc_build_cache" {
  provider = aws.us-west-2
  bucket   = aws_s3_bucket.pxc_build_cache.id

  transition_default_minimum_object_size = "all_storage_classes_128K"

  rule {
    id     = "30 Days Cleanup"
    status = "Enabled"

    filter {
      prefix = "jenkins-pxc"
    }

    expiration {
      days = 30
    }

    noncurrent_version_expiration {
      noncurrent_days = 1
    }

    abort_incomplete_multipart_upload {
      days_after_initiation = 1
    }
  }

  rule {
    id     = "90 Days Cleanup"
    status = "Enabled"

    filter {
      prefix = "jenkins-percona-xtrabackup"
    }

    expiration {
      days = 90
    }

    noncurrent_version_expiration {
      noncurrent_days = 1
    }
  }

  rule {
    id     = "pxc-build-cache 30 day cleanup"
    status = "Enabled"

    filter {
      prefix = ""
    }

    expiration {
      days = 30
    }

    noncurrent_version_expiration {
      noncurrent_days = 30
    }

    abort_incomplete_multipart_upload {
      days_after_initiation = 30
    }
  }
}

# ---------------------------------------------------------------------------
# pxb-backup-ci (us-west-2): bucket the PXB backup-test framework targets in
# CI. Fully private: access block on, ACLs disabled, SSE-C rejected.
# ---------------------------------------------------------------------------

import {
  provider = aws.us-west-2
  to       = aws_s3_bucket.pxb_backup_ci
  id       = "pxb-backup-ci"
}

resource "aws_s3_bucket" "pxb_backup_ci" {
  provider = aws.us-west-2
  bucket   = "pxb-backup-ci"

  tags = {
    "iit-billing-tag" = "pxb"
    team              = "xtrabackup"
    owner             = "tomislav.plavcic"
    ticket            = "PKG-1330"
  }

  lifecycle {
    prevent_destroy = true
  }
}

import {
  provider = aws.us-west-2
  to       = aws_s3_bucket_server_side_encryption_configuration.pxb_backup_ci
  id       = "pxb-backup-ci"
}

resource "aws_s3_bucket_server_side_encryption_configuration" "pxb_backup_ci" {
  provider = aws.us-west-2
  bucket   = aws_s3_bucket.pxb_backup_ci.id

  rule {
    apply_server_side_encryption_by_default {
      sse_algorithm = "AES256"
    }
    bucket_key_enabled       = false
    blocked_encryption_types = ["SSE-C"]
  }
}

import {
  provider = aws.us-west-2
  to       = aws_s3_bucket_ownership_controls.pxb_backup_ci
  id       = "pxb-backup-ci"
}

resource "aws_s3_bucket_ownership_controls" "pxb_backup_ci" {
  provider = aws.us-west-2
  bucket   = aws_s3_bucket.pxb_backup_ci.id

  rule {
    object_ownership = "BucketOwnerEnforced"
  }
}

import {
  provider = aws.us-west-2
  to       = aws_s3_bucket_public_access_block.pxb_backup_ci
  id       = "pxb-backup-ci"
}

resource "aws_s3_bucket_public_access_block" "pxb_backup_ci" {
  provider = aws.us-west-2
  bucket   = aws_s3_bucket.pxb_backup_ci.id

  block_public_acls       = true
  block_public_policy     = true
  ignore_public_acls      = true
  restrict_public_buckets = true
}

# ---------------------------------------------------------------------------
# pxb-build-cache (us-west-2): build artifact cache used by every PXB build
# and test pipeline. No lifecycle rules, access block fully open.
# ---------------------------------------------------------------------------

import {
  provider = aws.us-west-2
  to       = aws_s3_bucket.pxb_build_cache
  id       = "pxb-build-cache"
}

resource "aws_s3_bucket" "pxb_build_cache" {
  provider = aws.us-west-2
  bucket   = "pxb-build-cache"

  tags = {
    "iit-billing-tag" = "pxb"
    team              = "xtrabackup"
    owner             = "tomislav.plavcic"
    ticket            = "PKG-1330"
  }

  lifecycle {
    prevent_destroy = true
  }
}

import {
  provider = aws.us-west-2
  to       = aws_s3_bucket_server_side_encryption_configuration.pxb_build_cache
  id       = "pxb-build-cache"
}

resource "aws_s3_bucket_server_side_encryption_configuration" "pxb_build_cache" {
  provider = aws.us-west-2
  bucket   = aws_s3_bucket.pxb_build_cache.id

  rule {
    apply_server_side_encryption_by_default {
      sse_algorithm = "AES256"
    }
    bucket_key_enabled       = false
    blocked_encryption_types = ["NONE"]
  }
}

import {
  provider = aws.us-west-2
  to       = aws_s3_bucket_public_access_block.pxb_build_cache
  id       = "pxb-build-cache"
}

# Left fully open on purpose: PXB pipelines upload cache objects with public-read ACLs that consumers fetch anonymously.
#trivy:ignore:AVD-AWS-0086
#trivy:ignore:AVD-AWS-0087
#trivy:ignore:AVD-AWS-0091
#trivy:ignore:AVD-AWS-0093
resource "aws_s3_bucket_public_access_block" "pxb_build_cache" {
  provider = aws.us-west-2
  bucket   = aws_s3_bucket.pxb_build_cache.id

  block_public_acls       = false
  block_public_policy     = false
  ignore_public_acls      = false
  restrict_public_buckets = false
}

# ---------------------------------------------------------------------------
# rel-repo-cache (eu-west-1): cached upstream repository tag lists read by the
# release master's check-remote-repo jobs.
# ---------------------------------------------------------------------------

import {
  provider = aws.eu-west-1
  to       = aws_s3_bucket.rel_repo_cache
  id       = "rel-repo-cache"
}

# No public-access block: imported as-is, the bucket has never carried one.
#trivy:ignore:AVD-AWS-0086
#trivy:ignore:AVD-AWS-0087
#trivy:ignore:AVD-AWS-0091
#trivy:ignore:AVD-AWS-0093
resource "aws_s3_bucket" "rel_repo_cache" {
  provider = aws.eu-west-1
  bucket   = "rel-repo-cache"

  tags = {
    "iit-billing-tag" = "jenkins-cloud"
    team              = "release"
  }

  lifecycle {
    prevent_destroy = true
  }
}

import {
  provider = aws.eu-west-1
  to       = aws_s3_bucket_server_side_encryption_configuration.rel_repo_cache
  id       = "rel-repo-cache"
}

resource "aws_s3_bucket_server_side_encryption_configuration" "rel_repo_cache" {
  provider = aws.eu-west-1
  bucket   = aws_s3_bucket.rel_repo_cache.id

  rule {
    apply_server_side_encryption_by_default {
      sse_algorithm = "AES256"
    }
    bucket_key_enabled       = false
    blocked_encryption_types = ["NONE"]
  }
}

# ---------------------------------------------------------------------------
# percona-jenkins-artifactory (us-east-1): cross-fleet artifact and property
# file store referenced by every master's pipelines and IAM grants. Per-prefix
# expiry, access logged to the account's S3 access-log bucket.
# ---------------------------------------------------------------------------

import {
  to = aws_s3_bucket.jenkins_artifactory
  id = "percona-jenkins-artifactory"
}

# No public-access block: imported as-is, pipelines on every master read and write it with public-read object ACLs.
#trivy:ignore:AVD-AWS-0086
#trivy:ignore:AVD-AWS-0087
#trivy:ignore:AVD-AWS-0091
#trivy:ignore:AVD-AWS-0093
resource "aws_s3_bucket" "jenkins_artifactory" {
  bucket = "percona-jenkins-artifactory"

  tags = {
    "iit-billing-tag" = "jenkins"
  }

  lifecycle {
    prevent_destroy = true
  }
}

import {
  to = aws_s3_bucket_server_side_encryption_configuration.jenkins_artifactory
  id = "percona-jenkins-artifactory"
}

resource "aws_s3_bucket_server_side_encryption_configuration" "jenkins_artifactory" {
  bucket = aws_s3_bucket.jenkins_artifactory.id

  rule {
    apply_server_side_encryption_by_default {
      sse_algorithm = "AES256"
    }
    bucket_key_enabled       = false
    blocked_encryption_types = ["NONE"]
  }
}

import {
  to = aws_s3_bucket_logging.jenkins_artifactory
  id = "percona-jenkins-artifactory"
}

resource "aws_s3_bucket_logging" "jenkins_artifactory" {
  bucket = aws_s3_bucket.jenkins_artifactory.id

  target_bucket = local.s3_access_logs_bucket
  target_prefix = "logs/percona-jenkins-artifactory/"
}

import {
  to = aws_s3_bucket_lifecycle_configuration.jenkins_artifactory
  id = "percona-jenkins-artifactory"
}

resource "aws_s3_bucket_lifecycle_configuration" "jenkins_artifactory" {
  bucket = aws_s3_bucket.jenkins_artifactory.id

  transition_default_minimum_object_size = "all_storage_classes_128K"

  dynamic "rule" {
    for_each = {
      "30 Days Cleanup" = { prefix = "pxc-", noncurrent_days = 1 }
      "30 Days cloud-*" = { prefix = "cloud-", noncurrent_days = 1 }
      "30 Days fb-*"    = { prefix = "fb-", noncurrent_days = 1 }
      "30 Days psmdb-*" = { prefix = "psmdb-", noncurrent_days = 30 }
    }

    content {
      id     = rule.key
      status = "Enabled"

      filter {
        prefix = rule.value.prefix
      }

      expiration {
        days = 30
      }

      noncurrent_version_expiration {
        noncurrent_days = rule.value.noncurrent_days
      }
    }
  }

  rule {
    id     = "BUILDS"
    status = "Enabled"

    filter {
      prefix = "BUILDS/"
    }

    expiration {
      days = 30
    }

    noncurrent_version_expiration {
      noncurrent_days           = 30
      newer_noncurrent_versions = 2
    }
  }
}

# ---------------------------------------------------------------------------
# jenkins-percona-jobs-backup (us-east-1): Jenkins job XML backup target.
# Fully blocked from public access, access logged.
# ---------------------------------------------------------------------------

import {
  to = aws_s3_bucket.jenkins_jobs_backup
  id = "jenkins-percona-jobs-backup"
}

resource "aws_s3_bucket" "jenkins_jobs_backup" {
  bucket = "jenkins-percona-jobs-backup"

  tags = {
    "iit-billing-tag" = "jenkins-release"
    team              = "release"
  }

  lifecycle {
    prevent_destroy = true
  }
}

import {
  to = aws_s3_bucket_server_side_encryption_configuration.jenkins_jobs_backup
  id = "jenkins-percona-jobs-backup"
}

resource "aws_s3_bucket_server_side_encryption_configuration" "jenkins_jobs_backup" {
  bucket = aws_s3_bucket.jenkins_jobs_backup.id

  rule {
    apply_server_side_encryption_by_default {
      sse_algorithm = "AES256"
    }
    bucket_key_enabled       = false
    blocked_encryption_types = ["NONE"]
  }
}

import {
  to = aws_s3_bucket_logging.jenkins_jobs_backup
  id = "jenkins-percona-jobs-backup"
}

resource "aws_s3_bucket_logging" "jenkins_jobs_backup" {
  bucket = aws_s3_bucket.jenkins_jobs_backup.id

  target_bucket = local.s3_access_logs_bucket
  target_prefix = "logs/jenkins-percona-jobs-backup/"
}

import {
  to = aws_s3_bucket_public_access_block.jenkins_jobs_backup
  id = "jenkins-percona-jobs-backup"
}

resource "aws_s3_bucket_public_access_block" "jenkins_jobs_backup" {
  bucket = aws_s3_bucket.jenkins_jobs_backup.id

  block_public_acls       = true
  block_public_policy     = true
  ignore_public_acls      = true
  restrict_public_buckets = true
}

# ---------------------------------------------------------------------------
# percona-vm (us-east-1): PMM Server OVA images and docker image tarballs,
# served as a static website. The bucket ACL allows anonymous listing, the
# bucket policy allows anonymous reads of the OVA objects and admits the
# CloudTrail delivery path. Objects move to Standard-IA after 30 days and
# expire after 90.
# ---------------------------------------------------------------------------

import {
  to = aws_s3_bucket.percona_vm
  id = "percona-vm"
}

# No public-access block: the bucket is a public website (ACL, policy and website configuration below).
#trivy:ignore:AVD-AWS-0086
#trivy:ignore:AVD-AWS-0087
#trivy:ignore:AVD-AWS-0091
#trivy:ignore:AVD-AWS-0093
resource "aws_s3_bucket" "percona_vm" {
  bucket = "percona-vm"

  tags = {
    "iit-billing-tag" = "pmm"
    team              = "pmm"
    PerconaApproved   = "HD-26384"
  }

  lifecycle {
    prevent_destroy = true
  }
}

import {
  to = aws_s3_bucket_server_side_encryption_configuration.percona_vm
  id = "percona-vm"
}

resource "aws_s3_bucket_server_side_encryption_configuration" "percona_vm" {
  bucket = aws_s3_bucket.percona_vm.id

  rule {
    apply_server_side_encryption_by_default {
      sse_algorithm = "AES256"
    }
    bucket_key_enabled       = false
    blocked_encryption_types = ["NONE"]
  }
}

import {
  to = aws_s3_bucket_acl.percona_vm
  id = "percona-vm,public-read"
}

# Canned public-read, the same grant set CloudFormation declared: owner
# FULL_CONTROL plus AllUsers READ (bucket listing only).
#trivy:ignore:AVD-AWS-0092
resource "aws_s3_bucket_acl" "percona_vm" {
  bucket = aws_s3_bucket.percona_vm.id
  acl    = "public-read"
}

import {
  to = aws_s3_bucket_logging.percona_vm
  id = "percona-vm"
}

resource "aws_s3_bucket_logging" "percona_vm" {
  bucket = aws_s3_bucket.percona_vm.id

  target_bucket = local.s3_access_logs_bucket
  target_prefix = "logs/percona-vm/"
}

import {
  to = aws_s3_bucket_website_configuration.percona_vm
  id = "percona-vm"
}

resource "aws_s3_bucket_website_configuration" "percona_vm" {
  bucket = aws_s3_bucket.percona_vm.id

  index_document {
    suffix = "index.html"
  }

  error_document {
    key = "error.html"
  }
}

import {
  to = aws_s3_bucket_lifecycle_configuration.percona_vm
  id = "percona-vm"
}

resource "aws_s3_bucket_lifecycle_configuration" "percona_vm" {
  bucket = aws_s3_bucket.percona_vm.id

  transition_default_minimum_object_size = "varies_by_storage_class"

  rule {
    id     = "Remove after 3 months"
    status = "Enabled"

    filter {
      prefix = ""
    }

    transition {
      days          = 30
      storage_class = "STANDARD_IA"
    }

    expiration {
      days = 90
    }

    noncurrent_version_expiration {
      noncurrent_days = 1
    }

    abort_incomplete_multipart_upload {
      days_after_initiation = 1
    }
  }
}

data "aws_iam_policy_document" "percona_vm" {
  statement {
    sid       = "AllowPublicRead"
    effect    = "Allow"
    actions   = ["s3:GetObject"]
    resources = ["arn:aws:s3:::percona-vm/*.ova"]

    principals {
      type        = "AWS"
      identifiers = ["*"]
    }
  }

  statement {
    sid       = "AWSCloudTrailAclCheck20150319"
    effect    = "Allow"
    actions   = ["s3:GetBucketAcl"]
    resources = ["arn:aws:s3:::percona-vm"]

    principals {
      type        = "Service"
      identifiers = ["cloudtrail.amazonaws.com"]
    }
  }

  statement {
    sid       = "AWSCloudTrailWrite20150319"
    effect    = "Allow"
    actions   = ["s3:PutObject"]
    resources = ["arn:aws:s3:::percona-vm/AWSLogs/${data.aws_caller_identity.current.account_id}/*"]

    principals {
      type        = "Service"
      identifiers = ["cloudtrail.amazonaws.com"]
    }

    condition {
      test     = "StringEquals"
      variable = "s3:x-amz-acl"
      values   = ["bucket-owner-full-control"]
    }
  }
}

import {
  to = aws_s3_bucket_policy.percona_vm
  id = "percona-vm"
}

resource "aws_s3_bucket_policy" "percona_vm" {
  bucket = aws_s3_bucket.percona_vm.id
  policy = data.aws_iam_policy_document.percona_vm.json
}
