# Authentik bootstrap — first-time SAML source + Grafana OIDC client

The Authentik chart wrapper at `resources/addons/authentik/` is fully
self-bootstrapping. This runbook covers what gets created, how to verify
it, what to do when something drifts, and how to rotate the underlying
secrets. The original UI walk-through has been retired — see git history
(commit `952c753` and earlier) if you need to re-derive it.

## What the chart bootstraps

| Resource | Manifest | Purpose |
|---|---|---|
| `authentik-config` Secret | `templates/external-secret-config.yaml` | ESO splits the AWS Secrets Manager JSON bundle (`percona-ci-platform/authentik/config`) into 7 keys: `AUTHENTIK_SECRET_KEY`, `AUTHENTIK_BOOTSTRAP_PASSWORD`, `AUTHENTIK_BOOTSTRAP_TOKEN`, `AUTHENTIK_POSTGRESQL__PASSWORD`, and the `AUTHENTIK_OIDC_{GRAFANA,ARGOCD,HEADLAMP}_CLIENT_SECRET` trio |
| `authentik-saml` Secret + `authentik-saml-idp` ConfigMap | `templates/external-secret-saml.yaml` | SP signing keypair (cert + private_key) and Duo IdP metadata XML |
| `authentik-blueprint-{grafana,argocd,headlamp}` ConfigMaps | `templates/blueprint-*.yaml` | Declarative blueprints mounted into the worker, one per consumer. The grafana one also carries the shared pieces: the Duo SAML Source `duo-saml`, the group Property Mapping `duo-group-strip-dn`, and the identification-stage patch. Each declares its consumer's OAuth2 Provider + Application |
| `authentik-bootstrap-keypair` Job | `templates/bootstrap-keypair-job.yaml` | ArgoCD PostSync hook that uploads the SP keypair from `authentik-saml` into Authentik's `CertificateKeyPair` API as `duo-saml-sp`. Idempotent (GET by name first). The blueprint references it via `!Find` |

Adding a further OIDC client = a new `templates/blueprint-<consumer>.yaml`
ConfigMap (provider + application), a `random_password` key in
`terraform/authentik.tf`, the matching entry in
`templates/external-secret-config.yaml`, and the ConfigMap name appended to
`blueprints.configMaps` in `values.yaml`. ArgoCD and Headlamp followed this
pattern. See `docs/runbooks/authentik-blueprint-ops.md` for the apply gotchas
(worker restart required).

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
  || contains(groups[*], 'percona') && 'Viewer'
  || 'Viewer'
```
`GF_AUTH_GENERIC_OAUTH_ALLOWED_GROUPS` gates entry to `grafana_cd_admins
percona`. The `percona` Viewer leg is an accepted bootstrap risk (every
Duo-authenticated employee can read all dashboards and Loki datasources).
[authentication.md](../authentication.md) is canonical for the mapping and
the risk status. The proper close is a dedicated `grafana_cd_users` group.

Both consumers now run SSO-only:

- Grafana — `GF_AUTH_DISABLE_LOGIN_FORM=true` (closed in commit `684095d`).
- Authentik itself — `default-authentication-identification` stage has
  `user_fields: []`, so the username/email form is not rendered. With a
  single SAML source registered, the frontend auto-redirects directly to
  Duo. The akadmin local-login path is no longer reachable from the
  browser; recovery is documented below.

## Promote operator to internal

Authentik 2024.10+ enrolls source-federated users as `user_type=external`
by default, and externals are blocked from `/if/admin/` and `/if/user/`
with "Interface can only be accessed by internal users." For operators
who need to visit Authentik directly (debug a flow, inspect events,
rotate a token), promote them to `internal` after their first Duo
login enrolls the user record.

```bash
# 1. Identify the user pk (look for the email or username)
kubectl -n authentik exec deploy/authentik-worker -c worker -- /bin/sh -c \
  'curl -sS -H "Authorization: Bearer $AUTHENTIK_BOOTSTRAP_TOKEN" \
   "http://authentik-server.authentik.svc.cluster.local/api/v3/core/users/?include_groups=false"' \
  | jq -r '.results[] | "\(.pk)\t\(.username)\ttype=\(.type)"'

# 2. PATCH to internal (replace <PK> with the value from step 1)
kubectl -n authentik exec deploy/authentik-worker -c worker -- /bin/sh -c \
  'curl -sS -X PATCH \
     -H "Authorization: Bearer $AUTHENTIK_BOOTSTRAP_TOKEN" \
     -H "Content-Type: application/json" \
     -d "{\"type\": \"internal\"}" \
     "http://authentik-server.authentik.svc.cluster.local/api/v3/core/users/<PK>/"' \
  | jq '{username, type, is_active}'
```

Promoted users keep their groups and OIDC role mappings — only their
ability to access Authentik's own UI changes. **Don't promote
non-operators.** Non-admin users access Grafana via the OIDC redirect
and never need to load Authentik's UI; keeping them as `external`
matches least-privilege.

## Lockout recovery — akadmin

If SSO is broken (Duo down, SAML SP cert expired, blueprint mis-applied)
the only path back into Authentik admin is via the worker pod's `ak`
CLI. Two procedures, in order of preference.

**A. One-shot recovery URL (preferred — does not change any state).**

```bash
kubectl -n authentik exec deploy/authentik-worker -c worker -- \
  ak create_recovery_key 10 akadmin
# → https://auth.cd.percona.com/if/flow/default-recovery-flow/?token=...
```

The URL is valid for the given duration (minutes; default 60). Open it
in a clean browser session — it bypasses the identification stage
entirely and lands you in Authentik admin as `akadmin`. Token is
single-use.

**B. Reset akadmin password + log in via the unbroken flow.**

Only useful if the recovery flow itself is somehow broken.

```bash
kubectl -n authentik exec -it deploy/authentik-server -c server -- \
  ak changepassword akadmin
```

Then visit the recovery flow directly: `https://auth.cd.percona.com/if/flow/default-recovery-flow/`.
Note: the *authentication* flow at `/if/flow/default-authentication-flow/`
will still auto-redirect to Duo because of `user_fields: []`.

**Pre-requisites for either path:**

- The `authentik-worker` (procedure A) or `authentik-server` (procedure
  B) pod must be Running.
- You need `kubectl exec` access to the `authentik` namespace — same
  bar as cluster-admin in this cluster.

If both pods are down, the recovery story is "wait for ArgoCD to
re-roll them, or fix the underlying chart issue" — there is no API or
UI shortcut.

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
