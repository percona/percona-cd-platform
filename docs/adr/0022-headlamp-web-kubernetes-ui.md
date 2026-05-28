# 0022 — Headlamp as the web Kubernetes UI

**Status:** Accepted (2026-05-28)
**Related:** [ADR 0012](0012-authentik-saml-oidc-bridge.md) (Authentik OIDC/SAML bridge), [ADR 0019](0019-shared-alb-ssl-termination-for-jenkins-masters.md) (shared `jenkins-cd` ALB).

## Context

Operators need a Lens-equivalent web UI for the EKS cluster (consolidated tabular + log + graph views over deployments, cronjobs, events, logs, pods, ingresses, etc.) without distributing kubeconfigs and local `kubectl` setup. The realistic options surveyed in 2026-05:

- **Lens Desktop.** Was OSS, now a paid product under a different OSS license; sandbox builds exist but the live product is closed enough that distributing it to operators is a license problem. Not a fit for an internal platform.
- **Kubernetes Dashboard.** Read-mostly, basic resource browser, no log streaming or pod exec UX comparable to Lens. Adequate as an emergency fallback, not as the primary UI.
- **Octant.** Abandoned upstream (last release 2021). Off the table.
- **k9s.** TUI only, no tables/graphs/log panes side-by-side, single-operator-per-shell. Excellent as an admin tool, not a replacement for Lens.
- **Headlamp** (CNCF Sandbox, kubernetes-sigs/headlamp). Web + desktop, plugin system, active maintenance, native OIDC support, browser-deployable behind an ingress. Picked.

Headlamp's auth model lets us pick between two postures:

- **Shared ServiceAccount.** The Headlamp pod's SA is granted the cluster permissions it needs, and the OIDC step only gates frontend access. Simpler to wire, but the audit log records the SA on every action, and every user inherits the same RBAC.
- **Per-user OIDC passthrough.** Headlamp runs the OIDC browser round-trip against the IdP, then forwards the user's id_token as the Bearer to the kube-apiserver. Authorization is the user's own RBAC, keyed on OIDC group claims.

We already run Authentik as the SSO IdP (ADR 0012) and the EKS cluster has the external OIDC association slot free, so per-user passthrough costs about the same to wire and gives genuine per-user audit + group-mapped RBAC. That is the chosen posture.

## Decision

Deploy Headlamp as a managed addon on the cluster with **per-user OIDC passthrough**.

Components:

| Layer | Source | Purpose |
|---|---|---|
| Helm chart | `resources/addons/headlamp/` (wraps `headlamp` chart 0.42.0, kubernetes-sigs) | Headlamp Deployment + Service + ALB Ingress on the shared `jenkins-cd` group |
| EKS external OIDC | `terraform/eks-oidc-headlamp.tf` (`aws_eks_identity_provider_config.authentik_headlamp`) | Makes the apiserver trust Authentik-issued tokens |
| Authentik OAuth2 provider | `resources/addons/authentik/templates/blueprint-headlamp.yaml` (`headlamp-provider`) | Dedicated client + scope mappings + redirect URIs |
| ESO-synced OIDC secret | `resources/addons/headlamp/templates/external-secret-oidc.yaml` (`headlamp-oidc`) | OIDC client id/secret/issuer/callback env for the Headlamp backend |
| Per-user RBAC | `resources/addons/headlamp/templates/rbac.yaml` | ClusterRoleBindings on `oidc:`-prefixed groups |

EKS external OIDC association settings (in sync with the Authentik provider and the RBAC subject names):

```
issuer_url       = https://auth.cd.percona.com/application/o/headlamp/
client_id        = headlamp
username_claim   = email
username_prefix  = oidc:
groups_claim     = groups
groups_prefix    = oidc:
```

RBAC bindings:

| Group | ClusterRole | Scope |
|---|---|---|
| `oidc:percona` (all Percona staff) | `view` | Namespaced read-only |
| `oidc:grafana_cd_admins` (platform admins) | `cluster-admin` | Full cluster control |

## Load-bearing gotchas (all hit and fixed during the initial rollout)

These are the four non-obvious failure modes that gate getting Headlamp from "deployed" to "actually working". Recording them so the next maintainer does not rediscover them.

1. **Authentik's built-in OpenID email scope hardcodes `email_verified: false`.** Kubernetes' OIDC authenticator requires `email_verified: true` when `username_claim=email`; the apiserver otherwise rejects every token with `authentication.go:75 "Unable to authenticate the request" err="[invalid bearer token, oidc: email not verified, unknown]"`. Fix: bind a custom `email` scope mapping (`headlamp-email-verified`) that emits `True` to the headlamp provider in place of the built-in. Safe in this context because emails come from trusted corporate Duo/SAML SSO, not self-signup. Diagnostic shortcut: mint a token via `grant_type=client_credentials` against `/application/o/token/` with the provider's client secret, present it to `/apis/authentication.k8s.io/v1/selfsubjectreviews` on the apiserver endpoint, then read the apiserver log stream `kube-apiserver-*` (not `authenticator-*`, which is IAM only).

2. **Authentik rotates and revokes refresh tokens on first use (replay detection, no toggle).** Headlamp refreshes only within 10s of id_token expiry (`JWTExpirationTTL = 10 * time.Second` in `cmd/headlamp/backend/pkg/auth/auth.go`) and holds no single-flight lock, so the SPA's parallel API requests stampede a single cached refresh token. The first refresh wins and rotates; the rest replay the now-revoked token and get `invalid_grant`. Fix: set `access_token_validity` >= Headlamp's `-session-ttl` (24h here) so the refresh path effectively never fires during a session. `offline_access` stays bound as the 24h-boundary fallback (silent re-login from the live Authentik SSO session). Headlamp's own 10s grace cache of the old refresh token (`oldTokenTTL`) is defeated by Authentik's immediate revocation and cannot be relied on.

3. **Built-in `view` and `edit` ClusterRoles are namespaced-only by design.** Cluster-scoped resources (Nodes, PersistentVolumes, Namespaces, StorageClasses, CRDs, `metrics.k8s.io`) are deliberately excluded. A user bound to `edit` will authenticate fine but see "no permissions" on infra. For Lens-equivalent visibility, the platform admin group is bound to `cluster-admin`. Acknowledged trade-off: anyone added to the `grafana_cd_admins` Duo/Authentik group gets full cluster control via Headlamp. Split into a dedicated `k8s_cd_admins` group later if the linkage stops being acceptable.

4. **`ClusterRoleBinding.roleRef` is immutable.** Switching a binding's role requires delete-and-recreate; ArgoCD's `ServerSideApply` cannot mutate the field and `kubectl apply` errors. Workaround: rename the binding when re-aiming it (e.g. `headlamp-operators` -> `headlamp-admins`) so ArgoCD treats it as a new resource. With `PruneLast=true` on the Application's `syncOptions`, the new binding lands before the old one is removed, so there is no permission gap during the swap.

## Consequences

(+) **Lens-equivalent UX without local tooling.** Operators get tables/logs/graphs on every resource from a browser, gated by SSO + Duo + per-user RBAC.

(+) **Per-user audit trail.** Every cluster API call is attributed to `oidc:<email>` in the EKS audit log, not a shared ServiceAccount. Unauthorized actions become individually attributable.

(+) **No shared cluster-admin kubeconfig sprawled across machines.** The id_token is short-of-rotation but high-of-validity, only flows over HTTPS through the SSO chain, and is the user's own credential.

(-) **Cluster-admin is now reachable via SSO group membership.** Adding someone to `grafana_cd_admins` in Authentik/Duo gives them full cluster control. Compensated by the audit log + Duo MFA, but is a real escalation surface that the IdP admin must respect.

(-) **The id_token forwarded to the apiserver is long-lived (24h).** Compensated by SSO+Duo+RBAC gating and the apiserver re-validating signature/exp/aud/groups on every call. Acceptable for an internal cluster admin UI; not a pattern to reuse for end-user-facing services.

(-) **The single EKS external OIDC slot is now in use.** A second cluster-level OIDC consumer would require removing this association first. Future Headlamp-shaped consumers should reuse the existing association (multi-aud token contract) rather than asking for a second slot.

(-) **Authentik signing-cert expiry is now load-bearing for Headlamp too.** Both `authentik Self-signed Certificate` (signs id_tokens) and `duo-saml-sp` (signs the Authentik->Duo SP requests) expire **2027-05-06**. Calendar reminder for ~2027-04-06 and a cert-expiry PrometheusRule + runbook are an open follow-up (eks-hardening item #21).

## Implementation

The addon was wired across [#16](https://github.com/percona/percona-cd-platform/pull/16) (initial Helm chart + Authentik blueprint + Terraform OIDC association + RBAC) and [#17](https://github.com/percona/percona-cd-platform/pull/17) (ESO `template.engine` field removed for `external-secrets.io/v1`). The four gotchas above were closed in:

- [#20](https://github.com/percona/percona-cd-platform/pull/20) — `access_token_validity` 10m -> 24h (refresh-rotation replay fix).
- [#22](https://github.com/percona/percona-cd-platform/pull/22) — custom `email` scope mapping with `email_verified: true` (the load-bearing OIDC fix; without it nothing works).
- [#23](https://github.com/percona/percona-cd-platform/pull/23) — `oidc:grafana_cd_admins` rebound to `cluster-admin` (binding renamed because `roleRef` is immutable).

Reference points in `kubernetes-sigs/headlamp` source (these were decisive in diagnosis and are worth keeping a bookmark on):

- `backend/cmd/headlamp.go:960-1060` — `/oidc-callback` handler. State is checked from the query string, code is exchanged at the token endpoint, the id_token is verified against the JWKS, and the chunked auth cookie is set before the 303 redirect to `/auth?cluster=<cluster>`.
- `backend/pkg/auth/cookies.go:78` — `SetTokenCookie`. `HttpOnly: true`, `SameSite=Strict`, chunked at `chunkSize=3800`, `Path=/clusters/<cluster>`, scoped by `MaxAge=sessionTTL`.
- `backend/pkg/auth/auth.go:50` — `JWTExpirationTTL = 10 * time.Second` (the refresh trigger window) and the `oldTokenTTL = 10s` grace cache that Authentik's rotation defeats.
- `frontend/src/lib/auth.ts:38` — `getToken` returns `undefined` by default (HttpOnly cookies are not readable from JS); the backend reads the cookie on every proxy request and forwards the token as the Bearer.
- `frontend/src/lib/k8s/api/v1/clusterApi.ts:35` — `testAuth(cluster)` POSTs `selfsubjectrulesreviews`. A 401 or 403 there is what trips `RouteSwitcher.tsx`'s redirect to `/c/<cluster>/login`.

## Status of follow-ups

- Cert-expiry PrometheusRule + Grafana SAML cutover runbook (`docs/runbooks/grafana-saml-cutover.md`) for eks-hardening item #21: **open**. Both Authentik certs expire 2027-05-06; comfortable lead time.
- Dedicated `k8s_cd_admins` Duo group, split from `grafana_cd_admins`: **deferred**. Re-evaluate if the Grafana-admin and Kubernetes-admin populations diverge.
