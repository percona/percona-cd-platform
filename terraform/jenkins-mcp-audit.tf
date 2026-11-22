# Owner: platform
# Private S3 bucket for the audit archive of the jenkins-mcp gateway
# (resources/addons/jenkins-mcp/, chart value auditArchive.bucket). The gateway
# stores the audit record of every operate, manage and write call here as one
# JSON object under audit/write/YYYY/MM/DD/. Jenkins records the gateway's
# service account as the starter of every such build, and Loki keeps the audit
# line for 30 days, so this archive is the lasting link from a build to the
# person. Procedure: docs/runbooks/jenkins-mcp-operate.md, "Audit archive of
# write calls".
#
# A bucket of its own, apart from jenkins-mcp-exports.tf: the export bucket
# expires every object after 7 days, and the gateway may read and delete there.
# Here the gateway may only put under audit/write/ (iam-jenkins-mcp.tf), with
# no read, no list and no delete. Each put carries If-None-Match and the IAM
# policy requires that header, so S3 refuses to replace a stored record.
# Retention is the lifecycle expiry. Like the export bucket it has no
# versioning and no Object Lock.

locals {
  jenkins_mcp_audit_bucket         = "${local.cluster_name}-jenkins-mcp-audit"
  jenkins_mcp_audit_retention_days = 400
}

resource "aws_s3_bucket" "jenkins_mcp_audit" {
  bucket = local.jenkins_mcp_audit_bucket

  lifecycle {
    # Holds 400 days of audit records: a rename must never destroy the bucket.
    prevent_destroy = true
  }

  tags = merge(local.tags, {
    Name      = local.jenkins_mcp_audit_bucket
    component = "jenkins-mcp-audit"
  })
}

# BucketOwnerEnforced disables ACLs, IAM is the only access path.
resource "aws_s3_bucket_ownership_controls" "jenkins_mcp_audit" {
  bucket = aws_s3_bucket.jenkins_mcp_audit.id

  rule {
    object_ownership = "BucketOwnerEnforced"
  }
}

resource "aws_s3_bucket_public_access_block" "jenkins_mcp_audit" {
  bucket = aws_s3_bucket.jenkins_mcp_audit.id

  block_public_acls       = true
  block_public_policy     = true
  ignore_public_acls      = true
  restrict_public_buckets = true
}

resource "aws_s3_bucket_server_side_encryption_configuration" "jenkins_mcp_audit" {
  bucket = aws_s3_bucket.jenkins_mcp_audit.id

  rule {
    apply_server_side_encryption_by_default {
      sse_algorithm = "AES256"
    }
  }
}

resource "aws_s3_bucket_lifecycle_configuration" "jenkins_mcp_audit" {
  bucket = aws_s3_bucket.jenkins_mcp_audit.id

  rule {
    id     = "expire-audit"
    status = "Enabled"

    filter {
      prefix = ""
    }

    expiration {
      days = local.jenkins_mcp_audit_retention_days
    }

    abort_incomplete_multipart_upload {
      days_after_initiation = 1
    }
  }
}

data "aws_iam_policy_document" "jenkins_mcp_audit_bucket" {
  # Deny any non-TLS access (matches the export bucket).
  statement {
    sid       = "DenyInsecureTransport"
    effect    = "Deny"
    actions   = ["s3:*"]
    resources = [aws_s3_bucket.jenkins_mcp_audit.arn, "${aws_s3_bucket.jenkins_mcp_audit.arn}/*"]

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
}

resource "aws_s3_bucket_policy" "jenkins_mcp_audit" {
  bucket = aws_s3_bucket.jenkins_mcp_audit.id
  policy = data.aws_iam_policy_document.jenkins_mcp_audit_bucket.json

  # The policy must exist before the public-access-block evaluates writes.
  depends_on = [aws_s3_bucket_public_access_block.jenkins_mcp_audit]
}
