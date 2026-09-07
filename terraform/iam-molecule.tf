# Owner: platform
# EC2 read-only grant for the two molecule package-testing CI users. Their
# access keys let the QA pipelines create EC2 test VMs (molecule-tests.tf holds
# the VPC substrate). Jobs split across jenkins-s3-do and jenkins-spawn-user, so
# both need the same reads. Each user's other bindings are declared in
# iam-jenkins-spawn-user.tf and iam-jenkins-s3-do.tf. The hand-made policies
# both users carry stay unmanaged. This file owns both attachments of this
# grant, so neither of those files repeats them.
#
# The pinned ansible 13 bundles amazon.aws 10.x, whose ec2_instance reconciles
# tags through DescribeTags after launch. Earlier collections read tags off
# DescribeInstances only, so neither user needed the action before. DescribeTags
# is the one new grant. The other six restate reads the hand-made policies
# already hold, so this policy survives any future pruning of those.
#
# Six of the seven reads cannot be resource-scoped, and DescribeInstanceAttribute
# targets short-lived VMs whose ARNs are unknown ahead of time, so resources is
# ["*"].
#
# Attachments name the users as plain strings, so a user deleted out of band
# fails one attachment at apply time rather than every plan at refresh time.
# Separate resources rather than one for_each keep each state address stable.

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
