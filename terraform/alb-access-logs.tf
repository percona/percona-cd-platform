# Owner: platform
# Access-log bucket for both internet-facing ALBs, one prefix each.
# The enabling annotation lives on the argocd Ingress (argocd.tf) for
# jenkins-cd and on the jenkins-ingress addon Ingresses for
# jenkins-masters.
#
# Principal: regional ELB service account via aws_elb_service_account
# (no account ID in git). Legacy but supported; migrating to the
# recommended logdelivery.elasticloadbalancing.amazonaws.com principal
# must revalidate the working jenkins-cd delivery.
# SSE-S3 (AES256), not the LGTM CMK: ALB log delivery needs the bucket's
# default encryption to be a key it can use without a per-key grant, and
# AES256 is the friction-free supported option.

data "aws_elb_service_account" "main" {}

locals {
  alb_logs_bucket = "${local.cluster_name}-alb-logs"
}

resource "aws_s3_bucket" "alb_logs" {
  bucket = local.alb_logs_bucket

  tags = merge(local.tags, {
    Name = local.alb_logs_bucket
  })
}

# BucketOwnerEnforced disables ACLs, IAM is the only access path (matches
# the LGTM buckets). ALB log delivery works ACL-free via the bucket policy.
resource "aws_s3_bucket_ownership_controls" "alb_logs" {
  bucket = aws_s3_bucket.alb_logs.id

  rule {
    object_ownership = "BucketOwnerEnforced"
  }
}

resource "aws_s3_bucket_public_access_block" "alb_logs" {
  bucket = aws_s3_bucket.alb_logs.id

  block_public_acls       = true
  block_public_policy     = true
  ignore_public_acls      = true
  restrict_public_buckets = true
}

resource "aws_s3_bucket_server_side_encryption_configuration" "alb_logs" {
  bucket = aws_s3_bucket.alb_logs.id

  rule {
    apply_server_side_encryption_by_default {
      sse_algorithm = "AES256"
    }
  }
}

resource "aws_s3_bucket_lifecycle_configuration" "alb_logs" {
  bucket = aws_s3_bucket.alb_logs.id

  rule {
    id     = "expire-access-logs"
    status = "Enabled"

    filter {
      prefix = ""
    }

    expiration {
      days = 90
    }

    abort_incomplete_multipart_upload {
      days_after_initiation = 7
    }
  }
}

data "aws_iam_policy_document" "alb_logs" {
  # ALB log delivery PutObject. Path is fixed by AWS:
  # <prefix>/AWSLogs/<account-id>/*. One prefix per ALB group.
  statement {
    sid     = "AllowELBAccountPutObject"
    effect  = "Allow"
    actions = ["s3:PutObject"]
    resources = [
      "${aws_s3_bucket.alb_logs.arn}/jenkins-cd/AWSLogs/${data.aws_caller_identity.current.account_id}/*",
      "${aws_s3_bucket.alb_logs.arn}/jenkins-masters/AWSLogs/${data.aws_caller_identity.current.account_id}/*",
    ]

    principals {
      type        = "AWS"
      identifiers = [data.aws_elb_service_account.main.arn]
    }
  }

  # Deny any non-TLS access to the bucket (Well-Architected baseline,
  # matches the public-access-block posture).
  statement {
    sid       = "DenyInsecureTransport"
    effect    = "Deny"
    actions   = ["s3:*"]
    resources = [aws_s3_bucket.alb_logs.arn, "${aws_s3_bucket.alb_logs.arn}/*"]

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

resource "aws_s3_bucket_policy" "alb_logs" {
  bucket = aws_s3_bucket.alb_logs.id
  policy = data.aws_iam_policy_document.alb_logs.json

  # The policy must exist before the public-access-block evaluates writes.
  depends_on = [aws_s3_bucket_public_access_block.alb_logs]
}
