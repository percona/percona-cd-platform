# Owner: platform
# EC2 read-only grant for the molecule package-testing CI user (jenkins-s3-do),
# the long-lived IAM user whose access keys the QA package-testing pipelines
# use to create their EC2 test VMs (see molecule-tests.tf for the VPC
# substrate). The user predates this repo and stays unmanaged, as do its
# other hand-made policies; this file owns only the additional grant.
#
# The pipelines' pinned ansible 13 bundles amazon.aws 10.x, whose ec2_instance
# module reconciles instance tags through DescribeTags after launch; the
# previous collection generation read tags off DescribeInstances only, so the
# user never needed the action before. DescribeTags is the one genuinely new
# grant; the other six restate reads the user's hand-made policies already
# hold, so this policy self-documents the full read surface the playbooks use
# and survives any future pruning of those unmanaged policies. Six of the
# seven reads do not support resource-level scoping at all, and the one that
# does (DescribeInstanceAttribute) targets short-lived test VMs whose ARNs
# cannot be known ahead of time, so resources = ["*"].
#
# The attachment references the user by plain name on purpose: a data-source
# lookup would fail every plan of this state at refresh time if the unmanaged
# user were ever deleted out of band, while the plain reference confines that
# failure to this one attachment at apply time.

data "aws_iam_policy_document" "molecule_ec2_read" {
  statement {
    sid = "MoleculeEc2Read"
    actions = [
      "ec2:DescribeTags",
      "ec2:DescribeInstances",
      "ec2:DescribeInstanceStatus",
      "ec2:DescribeInstanceAttribute",
      "ec2:DescribeImages",
      "ec2:DescribeSubnets",
      "ec2:DescribeSecurityGroups",
    ]
    resources = ["*"]
  }
}

resource "aws_iam_policy" "molecule_ec2_read" {
  name        = "molecule-ec2-read"
  description = "EC2 read-only surface the amazon.aws ec2_instance module needs for molecule package-testing VMs"
  policy      = data.aws_iam_policy_document.molecule_ec2_read.json
  tags        = local.tags
}

resource "aws_iam_user_policy_attachment" "molecule_ec2_read" {
  user       = "jenkins-s3-do"
  policy_arn = aws_iam_policy.molecule_ec2_read.arn
}
