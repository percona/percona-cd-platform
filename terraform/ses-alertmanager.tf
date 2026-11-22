# Owner: platform

# SES sender for the Mimir Alertmanager. Alertmanager mails security alerts to
# one @percona.com mailbox (var.alertmanager_email_to). It can only send mail
# over SMTP, so it uses the SES SMTP endpoint with the SMTP credentials of the
# alertmanager-ses-smtp user. The mail goes out as alerts@notify.cd.percona.com,
# which reuses the DKIM, MAIL FROM and DMARC setup of ses-cve-agent.tf.
#
# The user is created here, so its access key is Terraform-managed, like
# iam-jenkins-artifactory-ps3.tf. The SMTP username and password and the
# recipient go to Secrets Manager with a write-only version. ESO renders them
# into the Alertmanager config Secret (resources/addons/mimir).
#
# Rotation: taint aws_iam_access_key.alertmanager_ses_smtp, bump
# secret_string_wo_version, apply, then restart mimir-alertmanager
# (docs/runbooks/alertmanager-email.md).

locals {
  alertmanager_mail_from = "alerts@${local.cve_agent_mail_domain}"
}

resource "aws_iam_user" "alertmanager_ses_smtp" {
  name = "alertmanager-ses-smtp"
  path = "/"

  # The org tagger adds both creator tags after the create, not Terraform.
  lifecycle {
    ignore_changes = [tags["PerconaCreatedBy"], tags["PerconaKeyCreatedBy"]]
  }
}

# SMTP sends authorize as ses:SendRawEmail. Send only as alerts@ and only to
# @percona.com. In the sandbox SES also authorizes against the recipient
# identity, so the percona.com identity is a resource here too.
data "aws_iam_policy_document" "alertmanager_ses_smtp" {
  statement {
    sid     = "SendAlertMail"
    actions = ["ses:SendRawEmail"]
    resources = [
      aws_sesv2_email_identity.cve_agent_notify.arn,
      "arn:aws:ses:${var.aws_region}:${data.aws_caller_identity.current.account_id}:identity/percona.com",
    ]
    condition {
      test     = "StringEquals"
      variable = "ses:FromAddress"
      values   = [local.alertmanager_mail_from]
    }
    condition {
      test     = "ForAllValues:StringLike"
      variable = "ses:Recipients"
      values   = ["*@percona.com"]
    }
  }
}

resource "aws_iam_user_policy" "alertmanager_ses_smtp" {
  name   = "ses-send-alertmanager"
  user   = aws_iam_user.alertmanager_ses_smtp.name
  policy = data.aws_iam_policy_document.alertmanager_ses_smtp.json
}

resource "aws_iam_access_key" "alertmanager_ses_smtp" {
  user = aws_iam_user.alertmanager_ses_smtp.name
}

resource "aws_secretsmanager_secret" "alertmanager_smtp" {
  name        = "${local.cluster_name}/mimir/alertmanager-smtp"
  description = "Alertmanager SES SMTP login and recipient as JSON (smtp_username, smtp_password, email_to)"

  tags = merge(local.tags, {
    app = "mimir"
  })
}

resource "aws_secretsmanager_secret_version" "alertmanager_smtp" {
  secret_id = aws_secretsmanager_secret.alertmanager_smtp.id

  # Write-only: the values stay out of the state file as a secret string. The
  # provider pushes a new value only when secret_string_wo_version changes. The
  # SMTP password is derived for the provider region, the region of the
  # SMTP endpoint in the Alertmanager config.
  secret_string_wo = jsonencode({
    smtp_username = aws_iam_access_key.alertmanager_ses_smtp.id
    smtp_password = aws_iam_access_key.alertmanager_ses_smtp.ses_smtp_password_v4
    email_to      = var.alertmanager_email_to
  })
  secret_string_wo_version = 1
}
