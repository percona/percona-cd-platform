# Owner: platform
# IAM policy for the in-cluster jenkins-endpoint-reconciler CronJob
# (resources/addons/jenkins-endpoint-reconciler/). Attached to the matching
# Pod Identity association in pod-identity.tf.
#
# ec2:DescribeInstances does NOT support resource-level permissions or
# tag-based authorization on the request — the only supported scoping is
# region (via aws:RequestedRegion). The reconciler needs to query every
# region where a Jenkins master lives (eu-west-1, eu-central-1, us-east-2,
# us-west-1, us-west-2). Cheaper to allow all regions than to maintain a
# per-region condition list that drifts as masters move. Action is read-only;
# blast radius is "an attacker who breaches this pod can list all EC2
# instances in the account".
#
# DescribeInstances cost is read-only API in EC2's tier; no rate-limit
# concern for one call per master per minute (10 masters x 1/min = 600/hr,
# well under EC2's API throttle).

data "aws_iam_policy_document" "jenkins_endpoint_reconciler" {
  statement {
    sid       = "DescribeInstances"
    actions   = ["ec2:DescribeInstances"]
    resources = ["*"]
  }
}

resource "aws_iam_policy" "jenkins_endpoint_reconciler" {
  name        = "${local.cluster_name}-jenkins-endpoint-reconciler"
  description = "Read-only ec2:DescribeInstances for ${local.cluster_name} jenkins-endpoint-reconciler"
  policy      = data.aws_iam_policy_document.jenkins_endpoint_reconciler.json
  tags        = local.tags
}
