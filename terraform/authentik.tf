# Authentik bootstrap — random secrets stored in AWS Secrets Manager and
# synced into the cluster by ESO (resources/addons/authentik/templates/
# external-secret-config.yaml). Authentik front-doors Duo SAML for
# Grafana (and later Jenkins masters, ArgoCD UI), exposing OIDC inward.
#
# Why a single Secrets Manager entry holding a JSON map instead of one
# secret per key:
#   - Authentik wires four runtime secrets at once (django SECRET_KEY,
#     bootstrap admin password, bundled-Postgres password, OIDC client
#     secret for Grafana). Keeping them together means a single
#     ExternalSecret reconcile + a single rotation path.
#   - Cost: AWS bills per-secret. One JSON blob = one $0.40/mo line item.
#
# Rotation:
#   - SECRET_KEY: rotate via `tofu taint random_password.authentik_secret_key`;
#     Authentik re-encrypts session data on next login. ~Annually.
#   - bootstrap_password: only used at first start; once a real admin user
#     is provisioned via Authentik UI/blueprints, this becomes inert.
#     Rotating it doesn't affect the running cluster.
#   - pg_password: rotate via `tofu taint random_password.authentik_pg_password`
#     + apply, then restart Postgres + Authentik server pods to pick up
#     the new password. Coordinated rotation, not zero-downtime.
#   - oidc_grafana_client_secret: rotate via taint + apply; Grafana picks
#     up the new value on next ESO refresh (1h). Plan a quick re-login
#     after rotation since in-flight code-exchange will fail.
#
# Cleanup-Lambda contract: tags include iit-billing-tag + PerconaKeep
# (per docs/eks-hardening.md and CLAUDE.md). The secret is application
# data; cleanup automation must not touch it.

resource "random_password" "authentik_secret_key" {
  length  = 50
  special = false # Authentik docs recommend alphanumeric for SECRET_KEY
}

resource "random_password" "authentik_bootstrap_password" {
  length  = 32
  special = false # printable in Authentik UI without escaping
}

resource "random_password" "authentik_pg_password" {
  length  = 32
  special = false # avoids quoting in Helm-rendered Postgres connection URLs
}

resource "random_password" "authentik_oidc_grafana_client_secret" {
  length  = 48
  special = false # OIDC client secret transmitted as basic-auth header
}

# AUTHENTIK_BOOTSTRAP_TOKEN: at first boot Authentik creates a long-lived API
# token for the akadmin user with this exact value. Required by the
# templates/bootstrap-keypair-job.yaml Job (which uploads the SP signing
# keypair via the Authentik API). Authentik's API rejects HTTP basic auth
# with username:password — only username:token works — so we cannot reuse
# AUTHENTIK_BOOTSTRAP_PASSWORD for API automation.
#
# Rotation: taint + apply, then restart authentik-server so the new token
# is picked up by the migration on next boot. The old token stays valid in
# the database until the operator deletes it from Directory → Tokens.
resource "random_password" "authentik_bootstrap_token" {
  length  = 60
  special = false # bearer token transported as basic-auth password
}

resource "aws_secretsmanager_secret" "authentik_config" {
  name        = "percona-ci-platform/authentik/config"
  description = "Authentik runtime secrets — synced to k8s by ESO into Secret 'authentik-config'"

  tags = merge(local.tags, {
    app = "authentik"
  })
}

resource "aws_secretsmanager_secret_version" "authentik_config" {
  secret_id = aws_secretsmanager_secret.authentik_config.id

  secret_string = jsonencode({
    AUTHENTIK_SECRET_KEY                 = random_password.authentik_secret_key.result
    AUTHENTIK_BOOTSTRAP_PASSWORD         = random_password.authentik_bootstrap_password.result
    AUTHENTIK_BOOTSTRAP_TOKEN            = random_password.authentik_bootstrap_token.result
    AUTHENTIK_POSTGRESQL__PASSWORD       = random_password.authentik_pg_password.result
    AUTHENTIK_OIDC_GRAFANA_CLIENT_SECRET = random_password.authentik_oidc_grafana_client_secret.result
  })
}
