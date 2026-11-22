# Owner: platform
# IAM policy for the jenkins-mcp gateway pod (resources/addons/jenkins-mcp/).
# Attached to the matching Pod Identity association in pod-identity.tf. Grants
# only what the export tools need on the exports bucket (jenkins-mcp-exports.tf):
# PutObject to stream a log/artifact in, GetObject to presign a download, and
# AbortMultipartUpload + DeleteObject so a failed or truncated streaming upload
# is cleaned up instead of leaving orphaned parts. Object-scoped to the one
# bucket; the pod has no other AWS access. See docs/adr/0039.
#
# A second policy lets the audit archive put objects under audit/write/ in the
# audit bucket (jenkins-mcp-audit.tf). It grants no read and no delete, and it
# requires the If-None-Match header, so the gateway cannot read back, list,
# remove or replace what it stored.

data "aws_iam_policy_document" "jenkins_mcp" {
  statement {
    sid = "ExportObjectReadWrite"
    actions = [
      "s3:PutObject",
      "s3:GetObject",
      "s3:AbortMultipartUpload",
      "s3:DeleteObject",
    ]
    resources = ["${aws_s3_bucket.jenkins_mcp_exports.arn}/*"]
  }
}

resource "aws_iam_policy" "jenkins_mcp" {
  name        = "${local.cluster_name}-jenkins-mcp"
  description = "Scoped S3 export access for the ${local.cluster_name} jenkins-mcp gateway (one bucket, objects only)"
  policy      = data.aws_iam_policy_document.jenkins_mcp.json
  tags        = local.tags
}

data "aws_iam_policy_document" "jenkins_mcp_audit" {
  statement {
    sid       = "AuditArchivePutOnly"
    actions   = ["s3:PutObject"]
    resources = ["${aws_s3_bucket.jenkins_mcp_audit.arn}/audit/write/*"]

    # S3 accepts the put only with an If-None-Match header, so a key that
    # exists is never replaced.
    condition {
      test     = "Null"
      variable = "s3:if-none-match"
      values   = ["false"]
    }
  }
}

resource "aws_iam_policy" "jenkins_mcp_audit" {
  name        = "${local.cluster_name}-jenkins-mcp-audit"
  description = "Put-only access to the audit/write prefix of the ${local.cluster_name} jenkins-mcp audit bucket"
  policy      = data.aws_iam_policy_document.jenkins_mcp_audit.json
  tags        = local.tags
}
