# Authentik signing-cert rotation

Authentik holds two certificate key-pairs in scope for this cluster, both currently expiring **2027-05-06**:

| Cert | Role | If it expires |
|---|---|---|
| `authentik Self-signed Certificate` | Signs every OIDC id_token (Headlamp, Grafana, ArgoCD) plus Authentik metadata | All SSO via OIDC stops; the apiserver and OIDC clients fail signature validation |
| `duo-saml-sp` (CN `grafana.cd.percona.com`, legacy naming) | Signs Authentik's SAML AuthnRequests to Duo | Duo step breaks; no logins through Authentik at all |

Duo's own IdP signing cert is not pinned in Authentik (`verification_kp=None` on the SAML Source); Authentik trusts Duo via the live metadata URL. Duo-side cert rotation is therefore handled by Duo and does not require action on our side unless they also change endpoints.

## When to run

- Roughly 30 days before expiry (calendar reminder, currently target ~2027-04-06).
- Emergency: if either cert is suspected exposed/compromised, run immediately.

## Pre-flight

- [ ] Confirm the bootstrap admin API token is in AWS Secrets Manager: `percona-ci-platform/authentik/config`, property `AUTHENTIK_BOOTSTRAP_TOKEN`.
- [ ] Confirm at least one alternative path to the cluster (an EKS access entry tied to your AWS principal, or AWS Console / `aws eks get-token`). The rotation has brief windows where SSO logins may be racy.
- [ ] Announce in `#opensource-jenkins` (or wherever ops chatter lives) that a brief SSO blip is possible.

## Cert 1: OIDC signing key (`authentik Self-signed Certificate`)

Used to sign every id_token. OIDC clients (Headlamp via the EKS external OIDC association, Grafana, ArgoCD) refetch the JWKS on signature mismatch, so a fresh signing key is picked up automatically as long as the issuer (`/application/o/<app>/`) keeps serving its `/jwks/` endpoint with the new key.

### Steps

1. **Generate a new signing key.** UI: *System -> Certificates -> Create*. Name: `authentik-jwt-signer-2027` (or any dated name). 4096-bit RSA, 2-year validity.

2. **Swap the signing key on every OAuth2 provider that uses the old cert.** Three to update: `headlamp-provider`, `grafana-provider`, `argocd-provider`. Either via the UI (*Providers -> select -> change `signing_key` -> save*) or the API:

   ```bash
   TOK=$(aws --profile "$AWS_PROFILE" --region us-east-1 \
         secretsmanager get-secret-value \
         --secret-id percona-ci-platform/authentik/config \
         --query SecretString --output text \
         | python3 -c 'import json,sys; print(json.load(sys.stdin)["AUTHENTIK_BOOTSTRAP_TOKEN"])')
   NEWKP=<pk-of-new-cert>   # from System -> Certificates UI

   for prov in headlamp-provider grafana-provider argocd-provider; do
     pk=$(curl -s -H "Authorization: Bearer $TOK" \
       "https://auth.cd.percona.com/api/v3/providers/oauth2/?search=$prov" \
       | python3 -c 'import json,sys; print(json.load(sys.stdin)["results"][0]["pk"])')
     curl -s -X PATCH -H "Authorization: Bearer $TOK" \
       -H 'Content-Type: application/json' \
       "https://auth.cd.percona.com/api/v3/providers/oauth2/$pk/" \
       -d "{\"signing_key\": \"$NEWKP\"}"
   done
   ```

3. **Codify the swap in the blueprints** so it survives an Authentik reinstall. In each of `resources/addons/authentik/templates/blueprint-headlamp.yaml`, `blueprint-grafana.yaml`, `blueprint-argocd.yaml`, change:

   ```yaml
   signing_key: !Find [authentik_crypto.certificatekeypair, [name, authentik-jwt-signer-2027]]
   ```

   Open a PR. ArgoCD syncs the ConfigMap; restart the `authentik-worker` afterwards so the blueprint re-applies (Authentik does not re-apply blueprint content changes without a worker boot; see [authentik-blueprint-ops](authentik-blueprint-ops.md) gotcha 2).

4. **Verify** that new tokens are signed with the new cert AND that the EKS apiserver still accepts them end-to-end:

   ```bash
   CS=$(aws --profile "$AWS_PROFILE" --region us-east-1 \
        secretsmanager get-secret-value \
        --secret-id percona-ci-platform/authentik/config \
        --query SecretString --output text \
        | python3 -c 'import json,sys; print(json.load(sys.stdin)["AUTHENTIK_OIDC_HEADLAMP_CLIENT_SECRET"])')

   IDT=$(curl -s -X POST https://auth.cd.percona.com/application/o/token/ \
         -d grant_type=client_credentials -d client_id=headlamp \
         --data-urlencode client_secret="$CS" -d scope='openid email profile' \
         | python3 -c 'import json,sys; print(json.load(sys.stdin)["id_token"])')

   # header has the new `kid` (matches the new cert's fingerprint)
   echo "$IDT" | cut -d. -f1 | base64 -d 2>/dev/null; echo

   # apiserver accepts -> 201
   EP=$(aws --profile "$AWS_PROFILE" --region us-east-1 \
        eks describe-cluster --name percona-ci-platform \
        --query 'cluster.endpoint' --output text)
   curl -s -o /dev/null -w 'HTTP %{http_code}\n' -k -X POST \
     "$EP/apis/authentication.k8s.io/v1/selfsubjectreviews" \
     -H "Authorization: Bearer $IDT" -H 'Content-Type: application/json' \
     -d '{"apiVersion":"authentication.k8s.io/v1","kind":"SelfSubjectReview"}'
   ```

   201 is good. Anything else: stop here; do not delete the old cert.

5. **Smoke-test the user-facing flows.** Open a fresh incognito window, log into Headlamp, Grafana, ArgoCD. All three should succeed.

6. **Decommission the old cert** after several hours of stable login traffic (refresh tokens in flight need to drain). UI: *System -> Certificates -> old cert -> Delete*. Authentik blocks deletion of a cert still referenced by anything, so any straggler provider keeps you safe.

## Cert 2: Duo SP signing key (`duo-saml-sp`)

Used to sign AuthnRequests sent to Duo. The new public cert must also be installed in Duo (Duo verifies our signature against the SP cert it stores).

### Steps

1. **Generate a new SP signing key** in Authentik. UI: *System -> Certificates -> Create*. Name: `duo-saml-sp-2027`. 2048-bit RSA, 2-year validity (matches the current cert; bumping to 4096 is fine if Duo accepts it).

2. **Register the new public cert in Duo.** Duo Admin Panel: find the SAML application backing the Authentik bridge, replace the SP public certificate (download it from Authentik *System -> Certificates -> new cert -> Download Certificate*), save.

3. **Swap the signing key on the Authentik SAML Source.** UI: *Sources -> Duo SSO -> signing_kp = new cert -> save*. Or via API:

   ```bash
   pk=$(curl -s -H "Authorization: Bearer $TOK" \
     'https://auth.cd.percona.com/api/v3/sources/saml/?slug=duo-saml' \
     | python3 -c 'import json,sys; print(json.load(sys.stdin)["results"][0]["pk"])')
   curl -s -X PATCH -H "Authorization: Bearer $TOK" \
     -H 'Content-Type: application/json' \
     "https://auth.cd.percona.com/api/v3/sources/saml/$pk/" \
     -d "{\"signing_kp\": \"<new-cert-pk>\"}"
   ```

4. **Verify.** From a fresh incognito window, log into Authentik through the Duo bridge (any of Headlamp/Grafana/ArgoCD will trigger it). The Duo redirect should still succeed and bounce you back signed in. Test with at least two browsers / accounts.

5. **Decommission the old cert** after a stable login window.

## Rollback

Old certs remain in Authentik until explicitly deleted; rollback is always available between steps 1-5 of either rotation:

- **Cert 1 rollback.** Swap providers back to the old cert's pk (UI or PATCH). Clients refetch JWKS on next signature mismatch and pick the old key back up.
- **Cert 2 rollback.** Swap the SAML Source back to the old `signing_kp` and revert the Duo SP metadata to the old cert.

## Related

- [ADR 0012 — Authentik SAML/OIDC bridge](../adr/0012-authentik-saml-oidc-bridge.md)
- [ADR 0022 — Headlamp web Kubernetes UI](../adr/0022-headlamp-web-kubernetes-ui.md)
- [authentik-blueprint-ops](authentik-blueprint-ops.md) — the worker-restart-to-reapply rule
- eks-hardening item #21 (cert-expiry PrometheusRule + this runbook)
