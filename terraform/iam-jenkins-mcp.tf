# Owner: platform
# IAM policy for the jenkins-mcp gateway pod (resources/addons/jenkins-mcp/).
# Attached to the matching Pod Identity association in pod-identity.tf. Grants
# only what the export tools need on the exports bucket (jenkins-mcp-exports.tf):
# PutObject to stream a log/artifact in, GetObject to presign a download, and
# AbortMultipartUpload + DeleteObject so a failed or truncated streaming upload
# is cleaned up instead of leaving orphaned parts. Object-scoped to the one
# bucket; the pod has no other AWS access. See docs/adr/0039.

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
