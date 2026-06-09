# Owner: platform
# LGTM object storage — three S3 buckets backed by a shared KMS CMK, one per
# LGTM component (Mimir blocks, Loki chunks, Tempo traces). Each component's
# chart writes to its own bucket only; pod-identity.tf scopes IAM to the
# matching bucket + this CMK.
#
# Cleanup-automation contract (docs/runbooks/cleanup-reapers.md):
#   - default_tags in providers.tf already merges iit-billing-tag + PerconaKeep
#     onto every aws_* resource this root creates. The explicit tags below are
#     belt-and-suspenders so any future cleanup Lambda that scans S3 / KMS /
#     IAM by tag will see the right pair on every LGTM resource.
#   - These buckets carry application data (metrics blocks, log chunks, trace
#     payloads). Lifecycle rules below handle retention; cleanup automation
#     must NOT delete them.
#
# Trivy notes (.trivyignore if needed):
#   - AWS-S017 (S3 access logging) intentionally NOT enabled. LGTM components
#     have their own request-level audit; an extra logging bucket per data
#     bucket triples object count for no incident-response value at this scale.

locals {
  lgtm_buckets = {
    mimir = {
      suffix         = "mimir-blocks"
      retention_days = 395 # ~13 months — compliance window for metrics
    }
    loki = {
      suffix         = "loki-chunks"
      retention_days = 30 # logs decay fast; longer retention belongs in S3 IA tier
    }
    tempo = {
      suffix         = "tempo-traces"
      retention_days = 14 # traces are sampling-heavy and short-retention by design
    }
  }
}

# Shared CMK for all three buckets. Separate from the cluster CMK
# (module.eks.kms_key_arn) and the AWS Backup CMK (aws_kms_key.backup) so
# rotations / IAM changes / incident isolation stay independent.
resource "aws_kms_key" "lgtm" {
  description             = "LGTM (Mimir/Loki/Tempo) object-storage encryption — ${local.cluster_name}"
  deletion_window_in_days = 7
  enable_key_rotation     = true
  policy                  = data.aws_iam_policy_document.lgtm_kms.json

  tags = local.tags
}

resource "aws_kms_alias" "lgtm" {
  name          = "alias/${local.cluster_name}-lgtm"
  target_key_id = aws_kms_key.lgtm.key_id
}

# Default-deny key policy: only the account root retains kms:*. Per-component
# permissions are granted via aws_iam_policy.lgtm_component[*] in
# pod-identity.tf — that's the AWS-recommended split (key policy = account
# trust boundary, IAM = day-to-day access grants).
data "aws_iam_policy_document" "lgtm_kms" {
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

resource "aws_s3_bucket" "lgtm" {
  for_each = local.lgtm_buckets

  bucket = "${local.cluster_name}-${each.value.suffix}"

  # Object Lock previously enabled (COMPLIANCE 7d) as an anti-ransomware
  # belt-and-braces. Removed 2026-05-08: Loki's bundled S3 client (AWS SDK Go v1)
  # does not emit Content-MD5 or x-amz-checksum-* headers on PutObject,
  # which Object Lock requires (`InvalidRequest: Content-MD5 OR
  # x-amz-checksum- HTTP header is required for Put Object requests with
  # Object Lock parameters`). Every Loki ingester PutObject was rejected,
  # ingesters never became Ready, the ring never quorated, and the read
  # path stayed dead. Tempo's SDK has the same risk class.
  #
  # Retention is handled by `aws_s3_bucket_lifecycle_configuration.lgtm`
  # below (per-component `expiration.days`), which is the right tool for
  # log/metric/trace chunks (constant churn, short shelf life).

  tags = merge(local.tags, {
    Name      = "${local.cluster_name}-${each.value.suffix}"
    component = each.key
  })
}

# BucketOwnerEnforced — disables ACLs, makes IAM the only access path.
# AWS default for new buckets since April 2023, set explicitly here so
# the hardening posture survives a future provider default change.
resource "aws_s3_bucket_ownership_controls" "lgtm" {
  for_each = local.lgtm_buckets

  bucket = aws_s3_bucket.lgtm[each.key].id

  rule {
    object_ownership = "BucketOwnerEnforced"
  }
}

resource "aws_s3_bucket_versioning" "lgtm" {
  for_each = local.lgtm_buckets

  bucket = aws_s3_bucket.lgtm[each.key].id

  versioning_configuration {
    status = "Enabled"
  }
}

resource "aws_s3_bucket_public_access_block" "lgtm" {
  for_each = local.lgtm_buckets

  bucket = aws_s3_bucket.lgtm[each.key].id

  block_public_acls       = true
  block_public_policy     = true
  ignore_public_acls      = true
  restrict_public_buckets = true
}

resource "aws_s3_bucket_server_side_encryption_configuration" "lgtm" {
  for_each = local.lgtm_buckets

  bucket = aws_s3_bucket.lgtm[each.key].id

  rule {
    apply_server_side_encryption_by_default {
      sse_algorithm     = "aws:kms"
      kms_master_key_id = aws_kms_key.lgtm.arn
    }
    # bucket_key_enabled saves ~99% of KMS request charges by caching the
    # data key per-bucket-per-day instead of one KMS call per object.
    bucket_key_enabled = true
  }
}

resource "aws_s3_bucket_lifecycle_configuration" "lgtm" {
  for_each = local.lgtm_buckets

  bucket = aws_s3_bucket.lgtm[each.key].id

  rule {
    id     = "${each.key}-retention"
    status = "Enabled"

    filter {
      prefix = ""
    }

    # Hard delete after the per-component retention window. LGTM components
    # don't expect to read their own buckets past retention, so a flat
    # expiration rule is correct (no IA/Glacier intermediate tiers, since
    # the cost saving is small at this volume and restore latency hurts).
    expiration {
      days = each.value.retention_days
    }

    # Versioning is for forensics (catch a runaway compactor), not long-term
    # retention. Drop noncurrent versions after a week.
    noncurrent_version_expiration {
      noncurrent_days = 7
    }

    # Abort dangling multipart uploads — Mimir/Loki/Tempo can leave them on
    # crash and they accumulate cost silently.
    abort_incomplete_multipart_upload {
      days_after_initiation = 7
    }
  }
}
