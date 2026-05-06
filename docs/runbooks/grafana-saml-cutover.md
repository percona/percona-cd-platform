# Grafana SAML/Duo cutover runbook

[HD-30780](https://perconadev.atlassian.net/browse/HD-30780) is the IT Ops
ticket that configures Duo SSO for `grafana.cd.percona.com` and creates the
two access groups `grafana_cd_users` (Viewer) and `grafana_cd_admins` (Admin).

The platform ships SAML config wired and dormant: Grafana renders no SAML
block, no ExternalSecret syncs, no Secrets Manager calls happen until
`var.grafana_saml_enabled` flips to `true`. This runbook is the single
sequence to flip it.

## Pre-flight (gate the merge)

- [ ] HD-30780 closed by IT Ops with three artifacts:
  - **IdP metadata URL** (Duo SSO XML) — single string, used as `var.grafana_saml_metadata_url`.
  - **SP private signing key** (PEM) — uploaded to AWS Secrets Manager.
  - **SP certificate** (PEM) — uploaded to AWS Secrets Manager.
- [ ] Duo groups `grafana_cd_users` + `grafana_cd_admins` exist and have at least one test member.
- [ ] You've verified the cluster is otherwise healthy (`argocd app list` all Synced/Healthy).

## Steps

### 1. Populate Secrets Manager

Two entries, both in `us-east-1`, both encrypted at rest with the default
SecretsManager CMK:

```bash
aws secretsmanager create-secret \
  --region us-east-1 \
  --name "percona-ci-platform/grafana/saml/certificate" \
  --secret-string file://saml-certificate.crt

aws secretsmanager create-secret \
  --region us-east-1 \
  --name "percona-ci-platform/grafana/saml/private_key" \
  --secret-string file://saml-private-key.pem
```

The External Secrets Operator already has IAM via Pod Identity
(`module.pod_identity_external_secrets` with `attach_external_secrets_policy`).
No extra grant needed.

### 2. Flip the TF flag

Edit `terraform/local.auto.tfvars` (gitignored on operator's box):

```hcl
grafana_saml_enabled      = true
grafana_saml_metadata_url = "https://sso.duosecurity.com/saml2/sp/.../metadata"
```

### 3. Apply

```bash
cd terraform && tofu plan -out=tfplan
tofu apply tfplan
```

Expected diff: cluster Secret annotations `grafana_saml_enabled` flips to
`"true"` and `grafana_saml_metadata_url` is populated. No other resources change.

### 4. ArgoCD reconciles Grafana

Within 3 minutes, ArgoCD picks up the new annotations on the cluster
Secret, the addons ApplicationSet re-renders the `grafana` Application's
`valuesObject` with `samlEnabled: "true"`, and the Helm chart now renders:

- An `ExternalSecret` (`grafana-saml`) that ESO resolves against the
  ClusterSecretStore. Within ~1 min the K8s `Secret/grafana-saml` exists
  with `certificate` + `private_key` keys.
- A `ConfigMap/grafana-saml-ini` with the `[auth.saml]` block.

Watch:

```bash
kubectl -n grafana get externalsecrets,secrets,configmaps | grep -i saml
kubectl -n grafana describe externalsecret grafana-saml
```

Expected: `Status: SecretSynced`. If `Status: SecretSyncedError`,
double-check the Secrets Manager paths match the spec exactly.

### 5. Force a Grafana pod roll

The Grafana Deployment doesn't auto-restart when the underlying Secret
changes. Roll it:

```bash
kubectl -n grafana rollout restart deploy/grafana
kubectl -n grafana rollout status  deploy/grafana
```

Pods come up reading the new `grafana.ini` + cert/key.

### 6. Smoke-test SP flow

```bash
# From a browser logged out of Grafana:
open https://grafana.cd.percona.com/login
```

- Click "Sign in with SAML".
- Browser redirects to Duo, prompts for credentials + 2FA.
- Duo redirects back; Grafana lands on the home dashboard.
- User's role: Admin if member of `grafana_cd_admins`, Viewer if member
  of `grafana_cd_users`, Viewer fallback otherwise (chart default
  `auto_assign_org_role`).

Smoke-check both groups end-to-end with two test users before announcing
the cutover.

## Rollback

If anything is wrong, revert in `local.auto.tfvars`:

```hcl
grafana_saml_enabled = false
```

`tofu apply` reverts the cluster Secret annotation, ArgoCD re-renders
Grafana without the SAML block, ExternalSecret + ConfigMap deleted, pods
roll, local-admin login is back. Total time-to-rollback: ~5 min.

The Secrets Manager entries stay populated (no harm); next cutover skips
step 1.

## Post-cutover

- [ ] Update `docs/observability.md` if the role mapping changes.
- [ ] Add the SAML cert expiry to the cert-rotation tracking sheet
  (or set up an Alertmanager rule alerting on `grafana_saml_certificate_expires_at`
  if Grafana exposes it; otherwise calendar reminder 30 days before expiry).
- [ ] Disable local-admin: edit `grafana/values.yaml` → `grafana.ini.auth.disable_login_form: true`.
  This blocks the username/password form once SAML works (defense-in-depth).
- [ ] Close HD-30780 with a "configured + verified" comment.
