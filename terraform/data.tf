# Owner: platform
# Shared data sources. Keep narrow — single-line lookups only; complex
# computed values belong in locals.tf.

data "aws_caller_identity" "current" {}
data "aws_region" "current" {}

# Public hosted zone for *.cd.percona.com. ACM, external-dns, and the per-host
# `origin-<host>` records all hang off this lookup.
data "aws_route53_zone" "main" {
  name         = var.route53_zone_name
  private_zone = false
}

# Authentik SAML IdP metadata (Duo). Lives in SSM ParameterStore because
# it's public IdP config (the signing cert is the public half of the
# IdP signature). Populated one-time via `aws ssm put-parameter`; TF
# reads at apply time, base64-encodes into the cluster Secret annotation
# (see argocd.tf), and the Authentik chart wrapper decodes into a
# ConfigMap. Optional = empty value when var.authentik_saml_enabled =
# false means the parameter doesn't have to exist for non-SAML applies.
data "aws_ssm_parameter" "authentik_saml_idp_metadata" {
  count = var.authentik_saml_enabled ? 1 : 0
  name  = "/${local.cluster_name}/authentik/saml/idp_metadata"
}
