# Authentik bootstrap — first-time SAML source + Grafana OIDC client

The Authentik chart wrapper at `resources/addons/authentik/` is fully
self-bootstrapping. This runbook covers what gets created, how to verify
it, what to do when something drifts, and how to rotate the underlying
secrets. The original UI walk-through has been retired — see git history
(commit `952c753` and earlier) if you need to re-derive it.

## What the chart bootstraps

| Resource | Manifest | Purpose |
|---|---|---|
| `authentik-config` Secret | `templates/external-secret-config.yaml` | ESO splits the AWS Secrets Manager JSON bundle (`percona-ci-platform/authentik/config`) into 5 keys: `AUTHENTIK_SECRET_KEY`, `AUTHENTIK_BOOTSTRAP_PASSWORD`, `AUTHENTIK_BOOTSTRAP_TOKEN`, `AUTHENTIK_POSTGRESQL__PASSWORD`, `AUTHENTIK_OIDC_GRAFANA_CLIENT_SECRET` |
| `authentik-saml` Secret + `authentik-saml-idp` ConfigMap | `templates/external-secret-saml.yaml` | SP signing keypair (cert + private_key) and Duo IdP metadata XML |
| `authentik-blueprint-grafana` ConfigMap | `templates/blueprint-grafana.yaml` | Declarative blueprint mounted into the worker — declares the Duo SAML Source `duo-saml`, the SAML groups Property Mapping `duo-groups-to-authentik`, the Grafana OAuth2 Provider `grafana`, and the Grafana Application |
| `authentik-bootstrap-keypair` Job | `templates/bootstrap-keypair-job.yaml` | ArgoCD PostSync hook that uploads the SP keypair from `authentik-saml` into Authentik's `CertificateKeyPair` API as `duo-saml-sp`. Idempotent (GET by name first). The blueprint references it via `!Find` |

Adding Jenkins / ArgoCD UI as further OIDC clients = appending two
entries (provider + application) to `templates/blueprint-grafana.yaml`
and a new ExternalSecret for the new `client_secret`.

## Verification — healthy install

```bash
# 1. Pods Running
kubectl -n authentik get pods
#    server, worker, postgresql Running 1/1

# 2. Ingress reachable
curl -fsS https://auth.cd.percona.com/-/health/live/   # → 200
curl -fsS https://auth.cd.percona.com/.well-known/openid-configuration | jq .issuer
#    → "https://auth.cd.percona.com/application/o/grafana/"

# 3. Bootstrap Job succeeded
kubectl -n authentik logs job/authentik-bootstrap-keypair
#    → "[bootstrap] keypair duo-saml-sp uploaded" or
#    → "[bootstrap] keypair duo-saml-sp already exists — nothing to do"

# 4. Blueprint applied
kubectl -n authentik exec deploy/authentik-worker -- /bin/sh -c \
  'curl -sS -H "Authorization: Bearer $AUTHENTIK_BOOTSTRAP_TOKEN" \
   "http://authentik-server.authentik.svc.cluster.local/api/v3/managed/blueprints/?search=grafana"' \
  | jq -r '.results[] | "\(.name): \(.status)"'
#    → "grafana-bridge: successful"

# 5. Grafana OIDC redirect chain
curl -sI https://grafana.cd.percona.com/login/generic_oauth | grep -i ^location
#    → location: https://auth.cd.percona.com/application/o/authorize/?client_id=grafana&...
```

## End-to-end SSO test (browser, manual)

1. Log out of any Authentik admin sessions (`https://auth.cd.percona.com/`).
2. Open `https://grafana.cd.percona.com/login` in a clean browser session.
3. Click **Sign in with Percona SSO** → 302 to Authentik authorize endpoint
   → 302 to Duo SSO → MFA prompt → Duo POSTs SAML response back to Authentik
   → Authentik mints OIDC code → 302 back to Grafana with `?code=...`
   → Grafana exchanges code, lands the user on the role mapped from Duo groups.

Group → role mapping in `resources/addons/grafana/values.yaml`:
```
contains(groups[*], 'grafana_cd_admins') && 'GrafanaAdmin'
  || contains(groups[*], 'grafana_cd_users') && 'Editor'
  || 'Viewer'
```
`GF_AUTH_GENERIC_OAUTH_ALLOWED_GROUPS` gates entry to `grafana_cd_admins
grafana_cd_users` only — non-members are denied at the OIDC step.

After the soak (~1 day), flip `GF_AUTH_DISABLE_LOGIN_FORM=true` in
`resources/addons/grafana/values.yaml` to remove the local-admin path.

## Troubleshooting

**User lands on Viewer despite being in `grafana_cd_admins`** — check, in
order:
1. Authentik UI → Events → Recent → click the relevant `LOGIN` event →
   Inspect — confirm the issued ID token's `groups` claim contains the
   expected group.
2. Authentik logs (`kubectl -n authentik logs deploy/authentik-server`)
   for the SAML response — confirm Duo sent the `groups` attribute.
3. The SAML Source Property Mapping `duo-groups-to-authentik` is attached
   to source `duo-saml`. Verify in the blueprint
   (`templates/blueprint-grafana.yaml`) — the entry is under the SAML
   Source's `user_property_mappings`.

**Blueprint stuck in `status=error`** — `kubectl -n authentik exec
deploy/authentik-worker -- ak shell` and run:
```python
from authentik.blueprints.v1.importer import Importer
text = open("/blueprints/mounted/cm-authentik-blueprint-grafana/grafana-bridge.yaml").read()
valid, logs = Importer.from_string(text).validate()
for l in logs: print(l.event, "|", l.attributes)
```
The `attributes.error` field has the serializer-level reason.

**Bootstrap Job in `CreateContainerConfigError`** — the `authentik-config`
Secret is missing one of the env-var keys it references. Force-sync the
ExternalSecret: `kubectl -n authentik annotate externalsecret
authentik-config force-sync=$(date +%s) --overwrite`.

## Rotation

| Secret | Rotation |
|---|---|
| `AUTHENTIK_SECRET_KEY` | `tofu taint random_password.authentik_secret_key && tofu apply` — Authentik re-encrypts session data on next login |
| `AUTHENTIK_BOOTSTRAP_PASSWORD` | Inert after first boot; rotate any time without restart |
| `AUTHENTIK_BOOTSTRAP_TOKEN` | Taint + apply, then `kubectl rollout restart deploy/authentik-worker` (the migration that updates the akadmin Token row runs there). Old token stays valid in DB until manually deleted via Directory → Tokens |
| `AUTHENTIK_POSTGRESQL__PASSWORD` | Coordinated: taint, apply, restart Postgres + Authentik server pods |
| `AUTHENTIK_OIDC_GRAFANA_CLIENT_SECRET` | Taint, apply; ESO refreshes within 1h; force ArgoCD refresh on the `grafana` app to roll the Grafana pod with the new secret. The blueprint resolves the new value via `!Env` on next worker boot |
| Duo IdP metadata | `aws ssm put-parameter --overwrite` then `tofu apply` — cluster-Secret annotation `authentik_saml_idp_metadata_b64` refreshes; ArgoCD redeploys the `authentik-saml-idp` ConfigMap; the SAML Source on Authentik's side keeps reading the file path so no UI step needed |
| SP signing cert + key | `openssl req -newkey rsa:2048 -nodes -keyout /tmp/k -x509 -days 1825 -subj '/CN=...' -out /tmp/c`; `aws secretsmanager put-secret-value --secret-id percona-ci-platform/authentik/saml/{certificate,private_key}`; force-sync the `authentik-saml` ExternalSecret; the bootstrap Job is *not* idempotent w.r.t. content, so delete the existing CertificateKeyPair `duo-saml-sp` from Authentik (Directory → Certificates) before re-running the Job, OR PATCH the keypair via API. Coordinate with Santiago — Duo's SP record carries the public cert; rotation needs a Duo-side update too |
