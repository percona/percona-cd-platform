# Owner: platform
#
# Deny-by-default GetSecretValue on out-of-band Secrets Manager secrets that
# are created outside Terraform and synced into the cluster by the External
# Secrets Operator. Only the ESO pod-identity role (plus operator-supplied
# break-glass patterns) may read them, which cuts each secret's read blast
# radius from "any principal with broad secretsmanager:GetSecretValue" (every
# AdministratorAccess holder) down to the one workload that needs it.
#
# The deny covers GetSecretValue ONLY: administrators can always edit or delete
# the policy to recover, and tofu never reads the values back (these secrets are
# not Terraform-managed, so there is no version to refresh), so the deny cannot
# break plan/apply. This mirrors aws_secretsmanager_secret_policy.authentik_config,
# extended to the remaining high-blast-radius secrets per finding B2 in
# docs/security-review-2026-05-07.md.
#
#   grafana/admin              Grafana server-admin basic-auth credential.
#                              Operators read the materialized k8s Secret
#                              (grafana-admin) or reset in-pod via grafana-cli,
#                              never the SM secret directly, so an ESO-only fence
#                              is safe. See docs/authentication.md.
#   authentik/saml/private_key SP signing key for SAML AuthnRequests to Duo. Only
#                              ESO reads it from SM; the bootstrap-keypair Job
#                              reads the materialized k8s Secret. The public half
#                              (authentik/saml/certificate) is intentionally left
#                              readable and is NOT fenced here.

locals {
  # logical key => Secrets Manager secret name (out-of-band, ESO-synced).
  fenced_secrets = {
    grafana_admin       = "percona-ci-platform/grafana/admin"
    authentik_saml_pkey = "percona-ci-platform/authentik/saml/private_key"
  }
}

data "aws_secretsmanager_secret" "fenced" {
  for_each = local.fenced_secrets
  name     = each.value
}

resource "aws_secretsmanager_secret_policy" "fenced" {
  for_each   = local.fenced_secrets
  secret_arn = data.aws_secretsmanager_secret.fenced[each.key].arn

  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Sid       = "DenyGetSecretValueExceptAllowlist"
      Effect    = "Deny"
      Principal = "*"
      Action    = "secretsmanager:GetSecretValue"
      Resource  = "*"
      Condition = {
        StringNotLike = {
          "aws:PrincipalArn" = concat(
            [module.pod_identity_external_secrets.iam_role_arn],
            var.fenced_secret_breakglass_arns
          )
        }
      }
    }]
  })
}
