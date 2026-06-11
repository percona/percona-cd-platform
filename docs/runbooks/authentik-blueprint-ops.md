# Authentik blueprint operations

Authentik blueprints (model `authentik_blueprints.blueprintinstance`) are the declarative YAML that Authentik reconciles into its DB. Several rough edges hit during the Headlamp rollout (PRs [#16](https://github.com/percona/percona-cd-platform/pull/16), [#17](https://github.com/percona/percona-cd-platform/pull/17), [#20](https://github.com/percona/percona-cd-platform/pull/20), [#22](https://github.com/percona/percona-cd-platform/pull/22)) are not obvious from the upstream docs; this runbook captures them so the next maintainer does not have to rediscover them.

## Mental model

- Blueprints are loaded from ConfigMaps mounted into the worker pod (label `app.kubernetes.io/component: blueprint` on the ConfigMap).
- The Authentik **worker** (not the server) applies blueprints into the DB. It discovers and applies on **boot**, plus a periodic reconcile, but **not** in response to a watched ConfigMap mutating in place.
- `!Find [model, [attr, value]]` resolves at apply time against the live DB. Entries within the same blueprint apply top-to-bottom, so a `scopemapping` referenced by name from a later provider entry must be defined earlier in the same file.

## Gotcha 1: a new ConfigMap blueprint is not auto-discovered

**Symptom.** A new blueprint ConfigMap was added. ArgoCD shows the Application Synced. But the OAuth2 provider / application / scope mapping defined in the blueprint never appears in the Authentik DB: `GET /api/v3/providers/oauth2/?search=<name>` returns empty `results`.

**Cause.** Authentik's blueprint discovery walks the mounted path on **worker boot**; it does not watch via inotify. The chart's mount uses the `..data` symlink pattern, and a brand-new ConfigMap added after the worker is already running is not picked up until the next boot or the next periodic reconcile (which can be hours away).

**Fix.**

```bash
kubectl --context percona-ci-platform -n authentik delete pod -l app.kubernetes.io/component=worker
kubectl --context percona-ci-platform -n authentik rollout status deploy/authentik-worker --timeout=120s
```

Then verify discovery + apply succeeded:

```bash
TOK=$(aws --profile "$AWS_PROFILE" --region us-east-1 \
      secretsmanager get-secret-value \
      --secret-id percona-ci-platform/authentik/config \
      --query SecretString --output text \
      | python3 -c 'import json,sys; print(json.load(sys.stdin)["AUTHENTIK_BOOTSTRAP_TOKEN"])')
curl -s -H "Authorization: Bearer $TOK" \
  "https://auth.cd.percona.com/api/v3/providers/oauth2/?search=<provider-name>"
```

The worker log shows `"Applying blueprint due to changed file"` for the relevant ConfigMap path during the boot sequence.

## Gotcha 2: a CONTENT change to an existing blueprint is not auto-reapplied

**Symptom.** A PR'd content change to an existing blueprint ConfigMap (e.g., `access_token_validity: minutes=10 -> hours=24`) merged. ArgoCD shows the Application Synced and the ConfigMap reflects the new value. But the Authentik provider still shows the OLD value in `/api/v3/providers/oauth2/`.

**Cause.** Same root as gotcha 1: a ConfigMap content change does not trigger an immediate re-discovery, and the periodic reconcile can be hours away. ArgoCD finishes its job once the ConfigMap is in sync; from then on it is Authentik's reconciliation cycle that gates apply.

**Fix.** Same worker restart as gotcha 1. Always verify post-restart via the Authentik API, not the ConfigMap, when judging whether a blueprint change took effect.

## Gotcha 3: built-in `email` scope mapping hardcodes `email_verified: false`

**Symptom.** OIDC clients with `username_claim=email` (the Kubernetes apiserver's OIDC authenticator is the canonical example) reject every token with `"oidc: email not verified"`. On EKS this is visible in the control-plane `kube-apiserver-*` log stream (not the IAM-only `authenticator-*` stream):

```
authentication.go:75 "Unable to authenticate the request"
  err="[invalid bearer token, oidc: email not verified, unknown]"
```

**Cause.** Authentik ships a managed `OpenID 'email'` scope mapping whose expression literally hardcodes `email_verified: False`. Kubernetes requires `email_verified: true` whenever `username_claim=email`.

**Fix.** Bind a CUSTOM `email` scope mapping to the affected provider in place of the built-in. Pattern lifted from `resources/addons/authentik/templates/blueprint-headlamp.yaml`:

```yaml
- model: authentik_providers_oauth2.scopemapping
  identifiers:
    name: headlamp-email-verified
  attrs:
    scope_name: email
    description: "OpenID email scope with email_verified=true (k8s OIDC)"
    expression: |
      return {
          "email": request.user.email,
          "email_verified": True,
      }
```

Reference it from the provider's `property_mappings` BY NAME (not by `scope_name`, which is now ambiguous because two mappings share `email`):

```yaml
property_mappings:
  - !Find [authentik_providers_oauth2.scopemapping, [scope_name, openid]]
  - !Find [authentik_providers_oauth2.scopemapping, [name, headlamp-email-verified]]
  - !Find [authentik_providers_oauth2.scopemapping, [scope_name, profile]]
  - !Find [authentik_providers_oauth2.scopemapping, [scope_name, offline_access]]
```

**Safety.** Asserting `email_verified: true` for everyone is acceptable here because emails come from trusted corporate Duo/SAML SSO, not self-signup. Do not blanket-override on a provider that accepts self-signup or external identity sources you do not control.

## Gotcha 4: refresh-token rotation is hardcoded

**Symptom.** An OIDC client receives `oauth2: "invalid_grant"` ("Refresh token is revoked" plus a `suspicious_request` event in Authentik's log) on a token refresh, even when the refresh token looks recent and the user is still signed in.

**Cause.** Authentik rotates AND immediately revokes the old refresh token on first use (replay detection). There is no provider-level toggle to disable this behaviour. Clients that fire concurrent refreshes with the same cached refresh token (Headlamp's SPA does this within the 10s pre-expiry window, with no single-flight lock) lose all but one of the requests to invalid_grant.

**Fix (config-level, no client patch required).** Set the provider's `access_token_validity` to match the client's session length so the refresh path effectively never fires during a normal session. Pattern:

```yaml
attrs:
  access_token_validity:  hours=24
  refresh_token_validity: days=30
```

24h on a token forwarded to the apiserver is acceptable because the apiserver re-validates signature/exp/aud/groups on every call. Do not reuse this pattern on end-user-facing services where shorter token lifetimes matter.

## Gotcha 5: ESO `external-secrets.io/v1` dropped `spec.target.template.engine`

**Symptom.** An ExternalSecret renders cleanly in Git, ArgoCD shows the Application Synced, but the Secret it should produce never appears. The consuming Pod stays in `CreateContainerConfigError` because `envFrom: secretRef: name: <foo>` cannot find the named Secret.

**Cause.** `external-secrets.io/v1` (the current stable API) dropped the `spec.target.template.engine: v2` field that was valid under `v1beta1`. ArgoCD's apply silently fails on the unknown field; the External Secrets controller never creates the Secret.

**Fix.** Remove the `engine:` line; v2 is the default. Validate any new ExternalSecret with server-side dry-run, which catches the unknown-field error that ArgoCD's apply path silently eats:

```bash
kubectl --context percona-ci-platform apply --dry-run=server \
  -f resources/addons/<addon>/templates/external-secret-*.yaml
```

## Gotcha 6: EKS allows exactly ONE external OIDC association per cluster

Headlamp currently consumes that slot via `terraform/eks-oidc-headlamp.tf` (`aws_eks_identity_provider_config.authentik_headlamp`). Any future cluster-level OIDC consumer must either reuse this association (multi-`aud` token contract on the Authentik side) or wait for the slot.

To swap the association out (control-plane mutation; takes ~10-40 minutes):

```bash
aws --profile "$AWS_PROFILE" --region us-east-1 eks \
  disassociate-identity-provider-config \
  --cluster-name percona-ci-platform \
  --identity-provider-config type=oidc,name=authentik-headlamp
```

The `aws_eks_identity_provider_config` Terraform resource has a separate apply lifecycle from the EKS module; if you ever depend on it from another standalone resource, depend on `module.eks.cluster_name` (a tag-only update), NOT on `module.eks` (which pulls the whole module into a `-target` apply and can drift managed node groups). Lesson banked from PR [#16](https://github.com/percona/percona-cd-platform/pull/16).

## End-to-end verification after any blueprint change

After a worker restart:

```bash
# 1. Provider exists / has expected fields
curl -s -H "Authorization: Bearer $TOK" \
  "https://auth.cd.percona.com/api/v3/providers/oauth2/?search=<name>" \
  | python3 -m json.tool | head -40

# 2. (k8s-consuming providers) end-to-end at the apiserver
EP=$(aws --profile "$AWS_PROFILE" --region us-east-1 \
     eks describe-cluster --name percona-ci-platform \
     --query 'cluster.endpoint' --output text)
CS=$(aws --profile "$AWS_PROFILE" --region us-east-1 \
     secretsmanager get-secret-value \
     --secret-id percona-ci-platform/authentik/config \
     --query SecretString --output text \
     | python3 -c 'import json,sys; print(json.load(sys.stdin)["AUTHENTIK_OIDC_HEADLAMP_CLIENT_SECRET"])')
IDT=$(curl -s -X POST https://auth.cd.percona.com/application/o/token/ \
      -d grant_type=client_credentials -d client_id=headlamp \
      --data-urlencode client_secret="$CS" -d scope='openid email profile' \
      | python3 -c 'import json,sys; print(json.load(sys.stdin)["id_token"])')
curl -s -k -X POST "$EP/apis/authentication.k8s.io/v1/selfsubjectreviews" \
  -H "Authorization: Bearer $IDT" -H 'Content-Type: application/json' \
  -d '{"apiVersion":"authentication.k8s.io/v1","kind":"SelfSubjectReview"}'
```

A 201 with `username: 'oidc:'` (or `oidc:<email>` for a real user token) means the OIDC chain works end-to-end. A 401 means the apiserver is still rejecting; check `authentication.go` in the control-plane log:

```bash
START=$(( ($(date +%s) - 120) * 1000 ))
aws --profile "$AWS_PROFILE" --region us-east-1 logs filter-log-events \
  --log-group-name /aws/eks/percona-ci-platform/cluster \
  --log-stream-name-prefix kube-apiserver-1 \
  --start-time "$START" --filter-pattern 'authentication.go' --limit 6 \
  --query 'events[].message' --output text
```

Audit-log signals on the apiserver side:

- `user=None code=401` on the request URI = token forwarded, apiserver rejected it (look in the apiserver log for the OIDC reason).
- No audit event at all for the URI = the Headlamp backend never forwarded the request (cookie not received, no token).

## Related

- [ADR 0012 — Authentik SAML/OIDC bridge](../adr/0012-authentik-saml-oidc-bridge.md)
- [ADR 0022 — Headlamp web Kubernetes UI](../adr/0022-headlamp-web-kubernetes-ui.md) — full context on the gotcha 3 / 4 fixes
- [authentik-bootstrap](authentik-bootstrap.md) — initial installation runbook
- [authentik-cert-rotation](authentik-cert-rotation.md) — the 2027-05-06 cert-expiry follow-up
