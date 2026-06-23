# Owner: platform
# Private S3 bucket for jenkins-mcp build-log/artifact exports. The MCP gateway
# (resources/addons/jenkins-mcp/) streams a log or artifact here, then returns a
# short-lived presigned GET so large payloads never traverse the MCP response.
# Objects are write-once under unguessable keys and auto-expire after 7 days;
# the bucket is fully private (presigned access only). IAM in iam-jenkins-mcp.tf,
# Pod Identity association in pod-identity.tf. See docs/adr/0039.
#
# SSE-S3 (AES256), not a CMK: a presigned GET from a browser carries no KMS
# grant, so SSE-S3 keeps the no-auth download working without a per-key policy.
# No versioning: keys are uuid-unique and never overwritten, so versions add
# only cost (retention is the lifecycle expiry, not Object Lock, which broke
# Loki PutObject on this account).

locals {
  jenkins_mcp_exports_bucket = "${local.cluster_name}-jenkins-mcp-exports"
}

resource "aws_s3_bucket" "jenkins_mcp_exports" {
  bucket = local.jenkins_mcp_exports_bucket

  tags = merge(local.tags, {
    Name      = local.jenkins_mcp_exports_bucket
    component = "jenkins-mcp-exports"
  })
}

# BucketOwnerEnforced disables ACLs, IAM is the only access path (matches the
# LGTM and alb-logs buckets).
resource "aws_s3_bucket_ownership_controls" "jenkins_mcp_exports" {
  bucket = aws_s3_bucket.jenkins_mcp_exports.id

  rule {
    object_ownership = "BucketOwnerEnforced"
  }
}

resource "aws_s3_bucket_public_access_block" "jenkins_mcp_exports" {
  bucket = aws_s3_bucket.jenkins_mcp_exports.id

  block_public_acls       = true
  block_public_policy     = true
  ignore_public_acls      = true
  restrict_public_buckets = true
}

resource "aws_s3_bucket_server_side_encryption_configuration" "jenkins_mcp_exports" {
  bucket = aws_s3_bucket.jenkins_mcp_exports.id

  rule {
    apply_server_side_encryption_by_default {
      sse_algorithm = "AES256"
    }
  }
}

resource "aws_s3_bucket_lifecycle_configuration" "jenkins_mcp_exports" {
  bucket = aws_s3_bucket.jenkins_mcp_exports.id

  rule {
    id     = "expire-exports"
    status = "Enabled"

    filter {
      prefix = ""
    }

    expiration {
      days = 7
    }

    abort_incomplete_multipart_upload {
      days_after_initiation = 1
    }
  }
}

data "aws_iam_policy_document" "jenkins_mcp_exports" {
  # Deny any non-TLS access (Well-Architected baseline, matches the
  # public-access-block posture).
  statement {
    sid       = "DenyInsecureTransport"
    effect    = "Deny"
    actions   = ["s3:*"]
    resources = [aws_s3_bucket.jenkins_mcp_exports.arn, "${aws_s3_bucket.jenkins_mcp_exports.arn}/*"]

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

resource "aws_s3_bucket_policy" "jenkins_mcp_exports" {
  bucket = aws_s3_bucket.jenkins_mcp_exports.id
  policy = data.aws_iam_policy_document.jenkins_mcp_exports.json

  # The policy must exist before the public-access-block evaluates writes.
  depends_on = [aws_s3_bucket_public_access_block.jenkins_mcp_exports]
}
