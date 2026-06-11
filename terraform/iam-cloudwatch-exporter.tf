# Owner: platform
# IAM policy for the in-cluster CloudWatch exporter, YACE
# (resources/addons/cloudwatch-exporter/). Attached to the matching
# Pod Identity association in pod-identity.tf. See ADR 0034.
#
# None of these actions support resource-level permissions: CloudWatch
# metric reads and the Resource Groups Tagging API are account-wide by
# design, and iam:ListAccountAliases is account-level. All read-only;
# blast radius is "an attacker who breaches this pod can read CloudWatch
# metrics, resource tags, and the account alias".
#
# cloudwatch:GetMetricStatistics is deliberately absent: YACE discovery
# jobs batch through GetMetricData only (static jobs are the sole
# GetMetricStatistics caller, and this deployment configures none).
# iam:ListAccountAliases keeps the account_alias label present from day
# one (granting it later would change every exported series' label set)
# and silences a per-scrape warning in the exporter log.

data "aws_iam_policy_document" "cloudwatch_exporter" {
  statement {
    sid = "CloudWatchRead"
    actions = [
      "cloudwatch:GetMetricData",
      "cloudwatch:ListMetrics",
      "tag:GetResources",
      "iam:ListAccountAliases",
    ]
    resources = ["*"]
  }
}

resource "aws_iam_policy" "cloudwatch_exporter" {
  name        = "${local.cluster_name}-cloudwatch-exporter"
  description = "Read-only CloudWatch metrics + tag discovery for ${local.cluster_name} cloudwatch-exporter (YACE)"
  policy      = data.aws_iam_policy_document.cloudwatch_exporter.json
  tags        = local.tags
}
