# Authentik bootstrap — first-time SAML source + Grafana OIDC client

> **As of branch `feat/authentik-bootstrap-blueprint`, the bootstrap is
> codified — the UI walkthrough below is the spec / fallback.** The chart now
> renders two extra resources:
>
> - `templates/blueprint-grafana.yaml` — ConfigMap blueprint declaring the
>   Duo SAML Source, the SAML groups Property Mapping, the Grafana OAuth2
>   Provider, and the Grafana Application. Mounted into the worker via
>   `authentik.blueprints.configMaps`; the worker reconciles on every boot
>   so UI drift gets repaired automatically.
> - `templates/bootstrap-keypair-job.yaml` — one-shot Job (ArgoCD PostSync
>   hook) that uploads the SP signing keypair from the `authentik-saml`
>   Secret into Authentik's CertificateKeyPair API. Idempotent — GETs by
>   name first. The blueprint references the keypair via
>   `!Find [authentik_crypto.certificatekeypair, [name, duo-saml-sp]]`.
>
> Adding Jenkins / ArgoCD UI as further OIDC clients = appending two
> entries (provider + application) to `templates/blueprint-grafana.yaml`.
>
> **Skip to §6 (Test) on a healthy install.** Sections §2–§5 below describe
> the manual equivalents and stay for debugging.

After the Authentik chart wrapper (`resources/addons/authentik/`) is healthy in
the cluster, the codified bootstrap above runs automatically. The sections
below describe the equivalent UI walk-through, retained for reference.

> **Prerequisites:**
> - PR-A1 (terraform/authentik.tf) applied — Secrets Manager has `percona-ci-platform/authentik/config`, IdP metadata in SSM, SP cert + key in Secrets Manager.
> - PR-A2 (this chart wrapper) merged — `kubectl get pods -n authentik` shows server, worker, postgres, redis Running.
> - DNS: `dig +short auth.cd.percona.com` returns the ALB hostname.
> - HTTPS reaches: `curl https://auth.cd.percona.com/-/health/live/` → 200.

The bootstrap admin password is in the ESO-synced Secret:
```bash
kubectl get secret -n authentik authentik-config \
  -o jsonpath='{.data.AUTHENTIK_BOOTSTRAP_PASSWORD}' | base64 -d
```

## 1. First login

Open `https://auth.cd.percona.com/if/admin/`. Log in:
- Username: `akadmin`
- Password: from the secret above

Authentik may prompt to set a recovery email; use the operator's Percona address.

## 2. Configure the Duo SAML source

The Duo IdP metadata is mounted at `/etc/authentik/saml-idp/idp-metadata.xml`
inside the server pod (from the `authentik-saml-idp` ConfigMap). The SP cert
and private_key live in the `authentik-saml` Secret (synced from AWS Secrets
Manager `percona-ci-platform/authentik/saml/{certificate,private_key}`).

Authentik UI: **Directory → Federation & Social login → Create → SAML Source**.

| Field | Value |
|---|---|
| Name | `duo-saml` |
| Slug | `duo-saml` |
| User matching | Identifier (email) |
| User path template | `goauthentik.io/sources/%(slug)s` |
| Pre-authentication flow | `default-source-pre-authentication` |
| Authentication flow | `default-source-authentication` |
| Enrollment flow | `default-source-enrollment` |
| SSO URL | `https://sso-b0cbd65b.sso.duosecurity.com/saml2/sp/DIS1DM3HXJYUVONGBVDH/sso` |
| Issuer | `https://sso-b0cbd65b.sso.duosecurity.com/saml2/sp/DIS1DM3HXJYUVONGBVDH/metadata` |
| Binding | POST |
| Allow IdP-initiated logins | off |
| NameID Policy | `urn:oasis:names:tc:SAML:1.1:nameid-format:emailAddress` |
| Verification certificate | (paste contents of `idp-metadata.xml` X509 cert; or upload as a `Certificate` first and select) |
| Signing keypair | upload from the `authentik-saml` Secret: cert + private_key |
| Delete temporary users after | leave default |

Save. Authentik exposes the SP-side metadata at:
`https://auth.cd.percona.com/source/saml/duo-saml/metadata/`

If Santiago needs the SP entityID / ACS URL on the Duo side, fetch that
metadata and send him the entityID + AssertionConsumerService URL.

## 3. Map the SAML `groups` attribute

**Directory → Property mappings → Create → SAML Source Property Mapping**.

| Field | Value |
|---|---|
| Name | `duo-groups-to-authentik` |
| SAML attribute name | `groups` |
| Object attribute | `groups` |
| Expression | empty (default — direct assignment) |

Then go back to the Duo SAML Source and add this property mapping under
"User property mappings".

## 4. Create the Grafana OIDC provider

**Applications → Providers → Create → OAuth2/OpenID Provider**.

| Field | Value |
|---|---|
| Name | `grafana` |
| Authentication flow | `default-authentication-flow` |
| Authorization flow | `default-provider-authorization-explicit-consent` |
| Client type | Confidential |
| Client ID | `grafana` |
| Client Secret | from the `authentik-config` Secret key `AUTHENTIK_OIDC_GRAFANA_CLIENT_SECRET`: `kubectl get secret -n authentik authentik-config -o jsonpath='{.data.AUTHENTIK_OIDC_GRAFANA_CLIENT_SECRET}' \| base64 -d` |
| Redirect URIs | `https://grafana.cd.percona.com/login/generic_oauth` (one line) |
| Signing key | `authentik Self-signed Certificate` (default) |
| Subject mode | Based on User's username |
| Include claims in id_token | on |
| Issuer mode | Each provider has a different issuer, based on the slug |
| Scopes | openid, profile, email, **groups**, offline_access |

## 5. Bind the provider to an Application

**Applications → Applications → Create**.

| Field | Value |
|---|---|
| Name | `Grafana` |
| Slug | `grafana` |
| Provider | `grafana` (the one created above) |
| Launch URL | `https://grafana.cd.percona.com/` |
| Open in new tab | off |
| Policy engine mode | Any |
| Group | (leave empty for v1; restrict later if needed) |

## 6. Test the chain

1. Log out of Authentik admin.
2. Open `https://grafana.cd.percona.com/login` in a clean browser session
   (this works once PR-A3 merges — until then Grafana doesn't show the OIDC button).
3. Click "Sign in with Percona SSO" → 302 to Authentik authorize endpoint
   → 302 to Duo SSO → MFA prompt → Duo POSTs SAML response back to Authentik
   → Authentik mints OIDC code → 302 back to Grafana with `?code=...`
   → Grafana exchanges code, lands user on the role mapped from their Duo groups.

For the first end-to-end test, ask Santiago to confirm the operator's account
is in `grafana_cd_admins` so you land on the Admin org. Then test a non-admin
user lands on Editor / Viewer.

## 7. Group → role mapping verification

Grafana's OIDC role mapping is driven by the JMESPath
`GF_AUTH_GENERIC_OAUTH_ROLE_ATTRIBUTE_PATH` in
`resources/addons/grafana/values.yaml`:
```
contains(groups[*], 'grafana_cd_admins') && 'GrafanaAdmin' || contains(groups[*], 'grafana_cd_users') && 'Editor' || 'Viewer'
```

If a user lands on `Viewer` despite being in `grafana_cd_admins`, check:
- Authentik's `groups` claim in the issued ID token (Authentik UI →
  Events → Recent → click an event → Inspect)
- The Duo SAML response actually contained the `groups` attribute
  (Authentik logs)
- The SAML Source Property Mapping (§3) is attached to the source

## Rotation

| Secret | Rotation |
|---|---|
| AUTHENTIK_SECRET_KEY | `tofu taint random_password.authentik_secret_key && tofu apply` — Authentik re-encrypts session data on next login |
| AUTHENTIK_BOOTSTRAP_PASSWORD | Inert after first login. Rotate any time without restart. |
| AUTHENTIK_POSTGRESQL__PASSWORD | Coordinated: taint, apply, restart Postgres + Authentik server pods |
| AUTHENTIK_OIDC_GRAFANA_CLIENT_SECRET | Taint, apply; ESO refreshes within 1h; force ArgoCD refresh on Grafana app to roll the Grafana pod with the new secret |
| Duo IdP metadata | `aws ssm put-parameter --overwrite` then `tofu apply` (cluster-Secret annotation refreshes); Authentik UI: re-import metadata into the SAML Source |
| SP signing cert + key | One-shot openssl regen (see PR-A1 history); `aws secretsmanager put-secret-value`; ESO refresh; Authentik UI: re-upload signing keypair on the SAML Source |
