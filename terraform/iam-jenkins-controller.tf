# IAM policy for the in-cluster Jenkins controller pilot (ps3-k8s). Attached to
# the matching EKS Pod Identity association in pod-identity.tf so the patched
# EC2 plugin (AWS SDK v2) resolves credentials via the default chain in-pod and
# can provision EC2 agents.
#
# Mirrors the EC2 masters' instance-profile "StartInstances" inline policy
# (terraform/modules/jenkins-master/main.tf -> master_start_instances): the EC2
# plugin's full provision / terminate / describe surface. These ec2:* actions do
# not support resource-level scoping (RunInstances tag conditions aside), so
# resources = ["*"] mirrors the masters; the pilot launches across the same
# master regions, so a per-region condition would just drift.

data "aws_iam_policy_document" "jenkins_controller" {
  statement {
    sid = "EC2Provision"
    actions = [
      "ec2:ModifySpotFleetRequest",
      "ec2:DescribeSpotFleetRequests",
      "ec2:DescribeSpotFleetInstances",
      "ec2:DescribeSpotInstanceRequests",
      "ec2:CancelSpotInstanceRequests",
      "ec2:GetConsoleOutput",
      "ec2:RequestSpotInstances",
      "ec2:RunInstances",
      "ec2:StartInstances",
      "ec2:StopInstances",
      "ec2:TerminateInstances",
      "ec2:CreateTags",
      "ec2:DeleteTags",
      "ec2:DescribeInstances",
      "ec2:DescribeKeyPairs",
      "ec2:DescribeRegions",
      "ec2:DescribeImages",
      "ec2:DescribeAvailabilityZones",
      "ec2:DescribeSecurityGroups",
      "ec2:DescribeSubnets",
    ]
    resources = ["*"]
  }

  # autoscaling for the ec2-fleet plugin (ASG-backed Graviton workers,
  # module.ps3_arm_fleet -> jenkins-ps3-arm-graviton). autoscaling Describe* do
  # not support resource-level scoping; the mutating actions are scoped to the
  # Jenkins ARM fleet ASGs (jenkins-*-arm-*).
  statement {
    sid = "Ec2FleetAutoScalingRead"
    actions = [
      "autoscaling:DescribeAutoScalingGroups",
      "autoscaling:DescribeScalingActivities",
      "autoscaling:DescribeAutoScalingInstances",
    ]
    resources = ["*"]
  }

  statement {
    sid = "Ec2FleetAutoScalingWrite"
    actions = [
      "autoscaling:UpdateAutoScalingGroup",
      "autoscaling:SetDesiredCapacity",
      "autoscaling:TerminateInstanceInAutoScalingGroup",
    ]
    resources = [
      "arn:aws:autoscaling:*:${data.aws_caller_identity.current.account_id}:autoScalingGroup:*:autoScalingGroupName/jenkins-*-arm-*",
    ]
  }

  # PassRole so the EC2 plugin can launch agents that carry a worker instance
  # profile (mirrors master_pass_role). Scoped to the Jenkins worker role naming
  # conventions (<short>-worker / <short>-slave); tighten to the pilot's exact
  # worker ARN once its EC2 cloud templates are wired (Stage 1).
  statement {
    sid     = "PassWorkerRole"
    actions = ["iam:PassRole"]
    resources = [
      "arn:aws:iam::${data.aws_caller_identity.current.account_id}:role/*-worker",
      "arn:aws:iam::${data.aws_caller_identity.current.account_id}:role/*-slave",
    ]
  }

  # The plugin lists roles / instance-profiles to populate its config UI dropdowns.
  statement {
    sid = "ListForUI"
    actions = [
      "iam:ListRoles",
      "iam:ListInstanceProfiles",
      "iam:ListInstanceProfilesForRole",
    ]
    resources = ["*"]
  }

  # The aws-secrets-manager-credentials-provider plugin (baked into the controller
  # image) polls ListSecrets to discover SM-backed credentials. ListSecrets has no
  # resource-level scoping, so it is account-wide metadata only (names / ARNs /
  # tags); reading a secret's value still needs GetSecretValue, granted per
  # credential namespace when those secrets are introduced. Without this the plugin
  # logs an AccessDenied warning every 60s.
  statement {
    sid       = "SecretsManagerList"
    actions   = ["secretsmanager:ListSecrets"]
    resources = ["*"]
  }

  # GetSecretValue IS resource-scopable, so it is scoped to the ps3 credential
  # namespace; this lets the SM-provider plugin read the values it lists. The
  # plugin needs no DescribeSecret (all metadata comes from ListSecrets) and reads
  # AWSCURRENT only (no version-id permissions). Backs the SM-backed credentials.
  statement {
    sid     = "SecretsManagerReadPs3"
    actions = ["secretsmanager:GetSecretValue"]
    resources = [
      "arn:aws:secretsmanager:us-east-1:${data.aws_caller_identity.current.account_id}:secret:percona-ci-platform/jenkins/ps3/*",
    ]
  }
}

resource "aws_iam_policy" "jenkins_controller" {
  name        = "${local.cluster_name}-jenkins-controller"
  description = "EC2-plugin provisioning for the in-cluster Jenkins controller pilot (${local.cluster_name})"
  policy      = data.aws_iam_policy_document.jenkins_controller.json
  tags        = local.tags
}
