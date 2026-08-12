# Owner: platform
# EC2 read-only grant for the molecule package-testing CI users, the long-lived
# IAM users whose access keys the QA package-testing pipelines use to create
# their EC2 test VMs (see molecule-tests.tf for the VPC substrate). The
# pipelines do not share one credential: jobs split across jenkins-s3-do and
# jenkins-spawn-user, so both need the same read surface. Both users predate
# this repo and stay unmanaged, as do their other hand-made policies. This file
# owns only the additional grant.
#
# The pipelines' pinned ansible 13 bundles amazon.aws 10.x, whose ec2_instance
# module reconciles instance tags through DescribeTags after launch; the
# previous collection generation read tags off DescribeInstances only, so
# neither user ever needed the action before. DescribeTags is the one genuinely
# new grant for both. The other six restate reads their hand-made policies
# already hold, so this policy self-documents the full read surface the
# playbooks use and survives any future pruning of those unmanaged policies.
# Six of the seven reads do not support resource-level scoping at all, and the
# one that does (DescribeInstanceAttribute) targets short-lived test VMs whose
# ARNs cannot be known ahead of time, so resources = ["*"].
#
# The attachments reference the users by plain name on purpose: a data-source
# lookup would fail every plan of this state at refresh time if an unmanaged
# user were ever deleted out of band, while the plain reference confines that
# failure to the one affected attachment at apply time. Each user gets its own
# attachment resource rather than one for_each block, so adding the second
# grant does not move the first one's state address.

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

resource "aws_iam_user_policy_attachment" "molecule_ec2_read_spawn_user" {
  user       = "jenkins-spawn-user"
  policy_arn = aws_iam_policy.molecule_ec2_read.arn
}
