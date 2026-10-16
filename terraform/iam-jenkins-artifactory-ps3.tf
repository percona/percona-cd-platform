# Owner: platform

# jenkins-artifactory-ps3, the IAM user behind the AWS_STASH credential on the
# in-cluster ps3 controller. ps3 used to carry a copy of jenkins-artifactory-user's
# key, shared with the production masters, and that copy is the one that was
# revoked as compromised. ps3 now gets its own user: a ps3 rotation touches no
# other master, and a ps3 leak is closed by deleting this one key.
#
# Unlike the imported legacy users, this user is created here, so its access key
# is Terraform-managed. The secret goes to Secrets Manager with a write-only
# version, under the percona-ci-platform/jenkins/ps3/ prefix that the ps3
# controller role already reads (iam-jenkins-controller.tf) and ESO can sync.
# The secret deliberately carries no jenkins:credentials:type tag: the Secrets
# Manager credentials provider on ps3 cannot build the AWS credential type, so
# it must skip this entry instead of surfacing a second AWS_STASH.
#
# Rotation: taint aws_iam_access_key.jenkins_artifactory_ps3, bump
# secret_string_wo_version, apply, then reload the credential on ps3
# (docs/runbooks/ps3-aws-stash.md). The old key stays valid until the apply
# deletes it, so reload ps3 right after the apply.
#
# jenkins-artifactory is hand-made and shared with other principals, so this
# file owns the attachment, not the document.

resource "aws_iam_user" "jenkins_artifactory_ps3" {
  name = "jenkins-artifactory-ps3"
  path = "/"

  # Own billing line, overriding the provider-wide default value.
  tags = {
    "iit-billing-tag" = "jenkins-artifactory-ps3"
  }

  # The org tagger adds both creator tags after the create, not Terraform.
  lifecycle {
    ignore_changes = [tags["PerconaCreatedBy"], tags["PerconaKeyCreatedBy"]]
  }
}

resource "aws_iam_user_policy_attachment" "jenkins_artifactory_ps3_artifactory" {
  user       = aws_iam_user.jenkins_artifactory_ps3.name
  policy_arn = "arn:aws:iam::${data.aws_caller_identity.current.account_id}:policy/jenkins-artifactory"
}

resource "aws_iam_access_key" "jenkins_artifactory_ps3" {
  user = aws_iam_user.jenkins_artifactory_ps3.name
}

resource "aws_secretsmanager_secret" "jenkins_ps3_aws_stash" {
  name        = "percona-ci-platform/jenkins/ps3/aws-stash"
  description = "AWS_STASH access key of jenkins-artifactory-ps3 as JSON (access_key_id, secret_access_key)"

  tags = merge(local.tags, {
    app = "jenkins-ps3-cd"
  })
}

resource "aws_secretsmanager_secret_version" "jenkins_ps3_aws_stash" {
  secret_id = aws_secretsmanager_secret.jenkins_ps3_aws_stash.id

  # Write-only: the key pair stays out of the OpenTofu state file as a secret
  # string. The provider only pushes a new value when secret_string_wo_version
  # changes, so every rotation must bump it. The aws_iam_access_key secret
  # attribute itself still lives in state, like every generated secret here.
  secret_string_wo = jsonencode({
    access_key_id     = aws_iam_access_key.jenkins_artifactory_ps3.id
    secret_access_key = aws_iam_access_key.jenkins_artifactory_ps3.secret
  })
  secret_string_wo_version = 1
}
