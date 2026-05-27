# EKS external OIDC identity provider — Authentik, for Headlamp per-user
# passthrough (resources/addons/headlamp). Headlamp runs the browser OIDC
# round-trip against Authentik and forwards the user's id_token as the
# Bearer to this cluster's apiserver, so authorization is the user's own
# RBAC (resources/addons/headlamp/templates/rbac.yaml binds the OIDC
# groups). This is what makes the apiserver trust that id_token.
#
# CONTROL-PLANE MUTATION — apply deliberately:
#   - Associating an OIDC provider reconfigures the apiserver and takes
#     ~10-40 min to reach ACTIVE; it does not interrupt existing API auth
#     (IAM access entries keep working) but it is not instant.
#   - EKS allows exactly ONE external OIDC provider per cluster. This slot
#     was verified empty on 2026-05-27 (`aws eks list-identity-provider-
#     configs` returned []). Adding a second association later requires
#     removing this one first.
#
# Token contract (verified against Headlamp source, kubernetes-sigs/headlamp):
#   - client_id MUST equal the id_token `aud`, which Authentik sets to the
#     provider client_id `headlamp` (blueprint-headlamp.yaml). Headlamp's
#     own verifier also checks aud == client_id, so both agree on `headlamp`.
#   - groups_claim `groups` must be present IN the id_token (not just
#     userinfo); the Authentik provider sets include_claims_in_id_token and
#     binds an explicit `groups` scope mapping to guarantee this.
#   - groups_prefix `oidc:` is a security control: it namespaces OIDC groups
#     so a token cannot claim a privileged built-in group (e.g.
#     `system:masters`) and inherit cluster-admin. The RBAC bindings in the
#     headlamp addon use the same `oidc:` prefix; keep the two in sync.
resource "aws_eks_identity_provider_config" "authentik_headlamp" {
  cluster_name = module.eks.cluster_name

  oidc {
    identity_provider_config_name = "authentik-headlamp"
    issuer_url                    = "https://auth.cd.percona.com/application/o/headlamp/"
    client_id                     = "headlamp"
    username_claim                = "email"
    username_prefix               = "oidc:"
    groups_claim                  = "groups"
    groups_prefix                 = "oidc:"
  }

  tags = local.tags
}
