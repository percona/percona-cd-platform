# Authentication — Duo → Authentik → OIDC bridge

Architecture decision and rejected alternatives in
[ADR 0012](adr/0012-authentik-saml-oidc-bridge.md). This doc covers
operational detail and recovery paths.

## Why a bridge

Percona's internal IdP is **Duo SSO** (FreeIPA-backed, owned by IT-Ops).
Internal apps that need SSO must federate to Duo via SAML 2.0.

Grafana OSS does not ship SAML — it's an Enterprise-only feature. Same
shape for ArgoCD UI and Jenkins masters: each speaks OIDC natively but
not SAML, or speaks SAML in a way Duo's SP-record convention doesn't
match.

So we run a single **Authentik** instance as a bridge. Authentik is a
SAML SP to Duo (federates outwards) and an OIDC IdP inwards. Each app
talks plain OIDC to Authentik, Authentik handles the SAML round-trip
once on the user's behalf, and group memberships propagate from Duo's
LDAP groups all the way to the application's role mapping.

## Architecture

```
            Browser
              │
              ▼  https://<app>.cd.percona.com
       ALB :443 (jenkins-cd ingress group)
              │
              ▼  302 to authorize endpoint (no app session)
       Authentik OIDC provider
              │
              ▼  302 to /source/saml/duo-saml (no Authentik session)
       Authentik SAML source 'duo-saml'
              │  (uses SP cert from authentik-saml Secret,
              │   sends AuthnRequest with SP entityID matching Duo's
              │   pre-registered SP record)
              ▼
       Duo SSO (sso-b0cbd65b.sso.duosecurity.com)
              │  user MFA
              ▼  signed SAML Response with `groups` attribute
              │  (FreeIPA group DNs:
              │   cn=grafana_cd_admins,cn=groups,...,dc=int,dc=percona,dc=com)
       Authentik /acs/
              │  group_property_mappings strips DN → bare CN
              │  Authentik creates/updates Group rows
              │  user_property_mappings populates email/name/etc
              ▼
       Authentik mints OIDC code, 302 back to <app>
              │
              ▼  POST /token (client_secret + PKCE verifier)
       <app> exchanges code → id_token (groups claim from `profile` scope)
              │
              ▼  ALLOWED_GROUPS gate + role mapping
       <app> session, role from group membership
```

## Components, in code

| Concern | Where |
|---|---|
| Authentik chart wrapper | `resources/addons/authentik/` |
| TF: Secrets Manager bundle, IAM, cluster-Secret annotations | `terraform/authentik.tf`, `terraform/argocd.tf` |
| Blueprint (SAML source, group mapping, OAuth providers, applications) | `resources/addons/authentik/templates/blueprint-grafana.yaml` |
| Bootstrap Job (uploads SP signing keypair via API) | `resources/addons/authentik/templates/bootstrap-keypair-job.yaml` |
| Grafana OIDC client config | `resources/addons/grafana/values.yaml` (`GF_AUTH_GENERIC_OAUTH_*`) |
| Operator runbook | `docs/runbooks/authentik-bootstrap.md` |

## Group propagation

Duo sends groups as full FreeIPA LDAP DNs:

```
cn=grafana_cd_admins,cn=groups,cn=accounts,dc=int,dc=percona,dc=com
cn=percona,cn=groups,cn=accounts,dc=int,dc=percona,dc=com
```

Authentik's SAML source pipeline (refactored in 2024.8) handles user and
group mappings via two distinct fields on the `SAMLSource` model:

- `user_property_mappings` — runs once on the assertion, populates User
  fields (email, name, etc.). It cannot rename or filter group
  memberships; the `groups` key in its return dict is merged with
  append-unique semantics onto the base properties dict.
- `group_property_mappings` — runs once **per group_id** at
  `request.context["group_id"]`, returns `{"name": "<new_name>"}`. The
  result becomes the Authentik `Group.name`. The user is then assigned
  to that Group via `GroupSAMLSourceConnection`.

Our blueprint binds a single group property mapping, `duo-group-strip-dn`,
that strips the leading `cn=<value>,...` to `<value>`. Result: the user
ends up in Authentik Groups named `grafana_cd_admins`, `percona`, etc.
The OIDC `profile` scope mapping (built-in default) emits these via
`request.user.groups.all()`, so the id_token's `groups` claim is the
list of bare CN names.

## Per-app role mapping

Each consuming app applies its own role rules against the `groups` claim.

### Grafana

```yaml
GF_AUTH_GENERIC_OAUTH_ALLOWED_GROUPS: "grafana_cd_admins percona"
GF_AUTH_GENERIC_OAUTH_ROLE_ATTRIBUTE_PATH: |
  contains(groups[*], 'grafana_cd_admins') && 'GrafanaAdmin'
    || contains(groups[*], 'percona') && 'Viewer'
    || 'Viewer'
```

`grafana_cd_admins` lands as Grafana server-admin (full curate rights).
`percona` (every Perconian) lands as Viewer. `ALLOWED_GROUPS` rejects
users in neither group at the token-validation step.

Bootstrap-phase trade-off: re-opening `percona` for Viewer access
re-introduces [security review chain C](security-review-2026-05-07.md)
(every Viewer can query Loki and read all-namespace auth/build
telemetry). Accepted as known exposure during bootstrap to skip the
IT-Ops coordination round-trip for a dedicated `grafana_cd_users` group.
The proper close is: ask Santiago for `grafana_cd_users`, swap `percona`
for it, and tighten the Loki datasource to Editor+ via Grafana 11 RBAC.

To add Editor capability later, register a Duo group (e.g.
`grafana_cd_editors`) and append a `contains()` clause before the
Viewer leg.

## Adding a new OIDC client

Roughly four steps:

1. **Provider + Application in the blueprint.** Append two entries to
   `resources/addons/authentik/templates/blueprint-grafana.yaml`:
   - `authentik_providers_oauth2.oauth2provider` with the new
     client_id, redirect_uri, and a client_secret resolved via
     `!Env <NEW_CLIENT_SECRET_VAR>`.
   - `authentik_core.application` binding the provider, with a slug.
2. **Secret in TF.** Add a new `random_password` to
   `terraform/authentik.tf` and include it in the
   `aws_secretsmanager_secret_version.authentik_config.secret_string`
   JSON bundle.
3. **ExternalSecret in the consumer chart.** New `external-secret-X.yaml`
   in the consumer addon's templates that pulls the new key from the
   `authentik/config` Secrets Manager bundle into a single-key k8s
   Secret the app mounts.
4. **App-side OIDC config.** Wire issuer/auth/token/userinfo URLs to
   `https://auth.cd.percona.com/application/o/<slug>/...`,
   `client_secret` via env-from-secret, scopes
   `openid profile email groups offline_access`, and the role mapping
   appropriate to that app.

## Recovery paths

| Failure mode | Recovery |
|---|---|
| App can't reach Authentik (Authentik down) | Apps with a local-admin emergency path: `kubectl exec` into the app pod, reset local password, `kubectl port-forward` to log in. Grafana: the login form stays disabled but basic auth is enabled with the ESO-pinned credential (`percona-ci-platform/grafana/admin` in Secrets Manager, Secret `grafana-admin`): call the API with `-u admin:<password>` through a port-forward (the public ALB works too, `enforce_domain` requires the right Host). If the pinned credential is ever lost, `grafana-cli admin reset-admin-password <pw>` in the pod, then update the SM secret to match. ArgoCD: re-enable the local admin per [argocd-admin-recovery](runbooks/argocd-admin-recovery.md). |
| Authentik admin locked out (akadmin password lost) | TF: `tofu taint random_password.authentik_bootstrap_password && tofu apply`, force ESO sync, restart authentik-server. Bootstrap migration sets the new password on next boot. |
| Authentik admin API token lost | Same shape with `random_password.authentik_bootstrap_token`. |
| Duo SP record drift (cert rotation, ACS URL change) | Coordinate with IT-Ops (`@zan` on Slack). SP entityID + ACS URL stay as defined in `templates/blueprint-grafana.yaml` and `docs/runbooks/authentik-bootstrap.md`. |
| Group memberships not refreshing for an existing Authentik user | The `default-source-authentication` flow has no User Write stage, so re-login doesn't re-sync groups. Workaround: delete the Authentik user via API; their next login goes through the `default-source-enrollment` flow which does run user_write + GroupUpdateStage. Long-term: define a custom auth flow with user_write, or rely on Duo group churn being slow enough to handle manually. |

## Constraints

- **Single Authentik replica.** Bundled Postgres is RWO; horizontal scale
  needs RDS first. Single replica is acceptable today since
  `priorityClassName: platform-system-critical` keeps Karpenter from
  preempting it, and short auth outages don't block already-issued
  sessions.
- **No automation for Duo SP record changes.** Cert rotation, ACS URL
  changes, group attribute mappings all require an HD JSM ticket or
  IT-Ops Slack. Track cert expiry separately (hardening item #21).
- **akadmin local-login is closed at the browser surface.** The
  blueprint patches `default-authentication-identification` to
  `user_fields: []` + `sources: [duo-saml-source]`. With one source and
  an empty user-fields list, the Authentik frontend auto-redirects
  straight to Duo — the username/email form is no longer rendered, so
  the form-based MFA bypass for akadmin is gone. Recovery for akadmin
  is exclusively via `ak create_recovery_key` in the worker pod (see
  [runbook](runbooks/authentik-bootstrap.md#lockout-recovery--akadmin));
  reaching that requires `kubectl exec` to the `authentik` namespace,
  i.e. cluster-admin level access.
