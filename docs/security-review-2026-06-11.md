# Authentik security posture review (2026-06-11)

**Snapshot:** commit `8e23d3c`. Live cluster + AWS state read 2026-06-10/11,
upstream advisory state read 2026-06-10.
**Scope:** the Authentik bridge end to end: the addon
(`resources/addons/authentik/`), its blueprints and bootstrap Job,
`terraform/authentik.tf`, the Secrets Manager bundle + ESO path, the three
OIDC consumers (Grafana, ArgoCD, Headlamp), the shared `jenkins-cd` ALB
front door, and upstream version/advisory currency.
**Method:** delta review against
[security-review-2026-05-07.md](security-review-2026-05-07.md). Every
carried-forward item was re-verified against live state (kubectl, AWS APIs)
or upstream sources, not the repo alone.
**Cross-references:** [authentication.md](authentication.md),
[ADR 0012](adr/0012-authentik-saml-oidc-bridge.md),
[ADR 0019](adr/0019-shared-alb-ssl-termination-for-jenkins-masters.md) (amended with this review),
[ADR 0022](adr/0022-headlamp-web-kubernetes-ui.md),
[eks-hardening.md](eks-hardening.md) items #16 and #21.

## Headline

Version currency is clean: the pinned app `2026.2.4` is the newest release
in its line, and every published upstream advisory is fixed at or below it.
The two biggest May findings that remain open are unchanged: no
restrictive NetworkPolicies anywhere (E1), and the all-keys Secrets Manager
bundle readable by any broad-SM-read principal with no resource policy and
no read alarm (B2/B3). Two would-be findings retired on live evidence: the
bundled Redis does not exist (the 2026.x chart dropped it), and the bundled
Postgres runs the maintained `docker.io/library/postgres` image, not the
frozen Bitnami one. The most actionable new finding is N1: Grafana's local
`admin` account remains reachable from the internet via basic auth even
though the login form is hidden.

## Live verification record (2026-06-10/11)

| Check | Result |
|---|---|
| Pods in `authentik` ns | `authentik-server`, `authentik-worker`, `authentik-postgresql-0` only. No Redis pod. |
| Running images | `ghcr.io/goauthentik/server:2026.2.4` (server + worker), `docker.io/library/postgres:17.9-bookworm` (PG, via the upstream chart's image override of the Bitnami subchart) |
| NetworkPolicies, cluster-wide | Exactly one: `authentik/authentik-postgresql` (Bitnami chart default). It allows ingress to 5432 from any pod and all egress. No default-deny anywhere. |
| `authentik` ns PSS labels | None (no pod-security admission) |
| server/worker securityContext | Pod and container both empty, ServiceAccount `default`. PG pod is hardened by chart defaults (runAsNonRoot 1001, readOnlyRootFilesystem, drop ALL, seccomp RuntimeDefault). |
| ExternalSecrets | `authentik-config`, `authentik-saml` both SecretSynced/Ready on the 1h interval |
| ArgoCD app sync | `authentik` Application Healthy but perpetually OutOfSync on the two ExternalSecrets (see N4) |
| ArgoCD local admin | `argocd-cm` `admin.enabled: "false"` confirmed live |
| Grafana auth config | `grafana.ini`: `disable_login_form = true`, `oauth_auto_login = false`, no `[auth.basic]` section, so basic auth is on (Grafana default). Chart-generated `admin` Secret present (see N1). |
| SM resource policy | `get-resource-policy` on `percona-ci-platform/authentik/config`: none (B2 open) |
| SM inventory | Exactly three `authentik` secrets (config, saml/certificate, saml/private_key), none in pending deletion (F1 closed) |
| Secret-read alarms | Zero CloudWatch alarms reference secrets (B3 open) |
| Shared ALB (`jenkins-cd`) | Listener 443-only with `ELBSecurityPolicy-TLS13-1-2-Res-PQ-2025-09`. `access_logs.s3.enabled=false`. `drop_invalid_header_fields=false`. No WAFv2 WebACL associated (F3 open). |
| EKS external OIDC | `authentik-headlamp` ACTIVE, `username_claim=email`, `groups_claim=groups`, both prefixed `oidc:` (matches `terraform/eks-oidc-headlamp.tf`) |

## Upstream currency (web, 2026-06-10)

- `2026.2.4` (2026-05-28) is the newest 2026.2.x release. It carries the
  same three GHSA backports as `2026.5.2`, released the same day
  (GHSA-c3m2-jqmq-pvp3, GHSA-wr38-7xg8-fqxr, GHSA-xp7f-xjjx-gwm8).
  CVE-2026-49448 (9.8 Source-stage bypass), CVE-2026-42849 (AutosubmitStage
  XSS), CVE-2026-40165 (SAML NameID comment injection), CVE-2026-40166
  (client_secret exposure to authenticated users), and CVE-2026-47201 are
  all fixed at or below our pin. No published advisory affects 2026.2.4.
- `2026.5.3` (2026-06-10) was reviewed item by item: SCIM enterprise,
  RADIUS, agent-connector, and OAuth logout-redirect fixes. None touch our
  deployed surface. Nothing security-relevant exists in 2026.5.x that is
  missing from 2026.2.4 today.
- Support runway: authentik supports two release lines at a time. With
  2026.5 out, 2026.2 falls out of security support when the next line
  ships (historically a quarterly cadence). Our blocker for 2026.5.x is
  the bundled-Postgres 17→18 datadir migration (noted in commit `9440cd9`).
  Plan that migration this quarter rather than under advisory pressure.
- Bitnami catalog freeze (Broadcom, 2025-08): handled upstream. The chart
  pins `docker.io/library/postgres:17.9-bookworm` over the Bitnami
  subchart, so the DB image still receives updates. The Bitnami postgresql
  chart skeleton itself (16.7.27) is frozen and will not see fixes. The
  stated long-term path stays RDS (values.yaml note) or the 2026.5.x chart.

## Status of 2026-05-07 items

| Item | May status | Now | Evidence |
|---|---|---|---|
| A (external attacker) | Dead end | Unchanged | Strict `redirect_uris`, ~190-bit secrets re-confirmed in blueprints |
| B1 broad-SM-read principal inventory | Open | Open | No inventory doc in repo |
| B2 SM resource policy | Open | **Open** | `get-resource-policy` returns none |
| B3 GetSecretValue alarm | Open | **Open** | Zero secret-related CloudWatch alarms |
| B4 akadmin form bypass | Addressed | Holds | `user_fields: []` in blueprint, worker boots from that ConfigMap |
| B5 bootstrap-token rotation cadence | Manual | Unchanged | Old tokens stay valid until deleted from Directory → Tokens (`terraform/authentik.tf` documents it) |
| C1 `percona` group in ALLOWED_GROUPS | Accepted risk | Unchanged (accepted) | `grafana/values.yaml` ROLE_ATTRIBUTE_PATH + ALLOWED_GROUPS |
| C2 Authentik log level | Open | **Closed** | `log_level: info` in values |
| C3 Loki single-tenant | Watch | Unchanged | `auth_enabled: false` |
| C4 Loki datasource Editor+ gate | Open | Open | Not implemented |
| D1 selfHeal on authentik app | Verify | **Closed** | `prune: true` + `selfHeal: true` in the addons ApplicationSet, confirmed in live app spec |
| D2 ConfigMap-mutation audit alert | Open | Open | Not found |
| D3 cluster-admin inventory runbook | Open | Open | Not found |
| E1 default-deny NetworkPolicy | Open | **Open (top item)** | The only cluster netpol is the permissive chart-generated PG one |
| E2 image digest pin | Open | Open | Tag pin only |
| E3 runAsNonRoot/readOnlyRootFilesystem | Open | Open, live-confirmed | server/worker securityContext empty, default SA. Upstream chart exposes the keys. |
| E4 LGTM gateway auth | Open | Partial | External master push path is bearer-authed (ADR 0013 alloy-gateway). In-cluster distributor ingestion stays unauthenticated single-tenant. |
| F1 old SP keypair in SM recovery window | Watch | **Closed** | SM inventory clean, no pending deletions |
| F2 client_secrets in PG dumps | Watch | Unchanged | Inherent to bundled PG, revisit at RDS migration |
| F3 WAF on admin UI | Open | Open | No WebACL associated with the shared ALB |
| F4 enrollment-spike alert | Open | Open | Not found |
| F5 cert-expiry alerting (hardening #21) | Open | Open | Both keypairs expire **2027-05-06**. #21 row updated to name them. |

## New findings (2026-06-11)

### N1: Grafana local admin reachable via basic auth (MEDIUM)

`disable_login_form: true` hides the form, but Grafana's HTTP basic auth
stays at its default (enabled) and the chart generates an `admin` password
into the `grafana` Secret (`admin.existingSecret: ""`). Anyone with that
Secret value can call `https://grafana.cd.percona.com/api/...` as server
admin from the internet, no Duo involved. This is the same account class
ArgoCD already closed (`admin.enabled: "false"`).
Fix: set `GF_AUTH_BASIC_ENABLED: "false"` in the Grafana values. Break-glass
stays available by reverting the value (GitOps) or `grafana-cli admin
reset-admin-password` + port-forward, as documented in
[authentication.md](authentication.md#recovery-paths).

### N2: dead `redis:` values block, and a retired finding class (INFO)

The 2026.x authentik chart has no Redis dependency (verified in the chart
archive: deps are `postgresql` and `authentik-remote-cluster` only) and no
Redis pod exists in the namespace. The `redis:` block in
`resources/addons/authentik/values.yaml` (including `auth.enabled: false`)
is inert, and the "unauthenticated in-cluster Redis" concern that block
implies is retired. Remove the block in a follow-up behavior-class PR
(kept out of this docs-only workset deliberately).

### N3: perpetual OutOfSync on the auth Application (LOW, ops)

ESO now defaults `spec.data[].remoteRef.nullBytePolicy: Ignore` on
ExternalSecrets. The addons ApplicationSet `ignoreDifferences` covers
`conversionStrategy` / `decodingStrategy` / `metadataPolicy` but not
`nullBytePolicy`, so the `authentik` (and `grafana`, `alloy-gateway`)
Applications sit permanently OutOfSync. Alarm fatigue on the auth app
masks real drift, which chain D relies on noticing. Fix: add
`.spec.data[].remoteRef.nullBytePolicy` to the jqPathExpressions list.

### N4: one Duo group is a triple-privilege bundle (MEDIUM, structural)

`grafana_cd_admins` grants Grafana server-admin, ArgoCD `role:admin`, and
Kubernetes `cluster-admin` (Headlamp RBAC). One IT-Ops group change is full
platform control. Compounding it, the ADR 0012 group-sync gap means
removing a member from the Duo group does not propagate to an existing
Authentik user on re-login (no User Write stage in
`default-source-authentication`): revocation requires deleting the
Authentik user via API. The `k8s_cd_admins` split is already deferred in
ADR 0022. This review re-raises it together with a documented revocation
procedure (delete user, not just the Duo group edit).

### N5: bootstrap Job hygiene (LOW)

`templates/bootstrap-keypair-job.yaml` runs with no securityContext and the
namespace `default` ServiceAccount, and it talks plain HTTP in-cluster to
`authentik-server` with the full-admin bootstrap token. The HTTP hop and
the token scope are documented trade-offs. E1's default-deny would bound
who else can reach that same unauthenticated-transport surface.

### N6: ALB front-door hygiene (LOW)

On the shared `jenkins-cd` ALB: access logs disabled (no request-level
audit of the SSO front door), `routing.http.drop_invalid_header_fields`
disabled, no WAF (F3). TLS posture itself is strong
(`ELBSecurityPolicy-TLS13-1-2-Res-PQ-2025-09`, 443-only with ssl-redirect).

### N7: `secret_string_wo` is now actionable (INFO)

The deferred ADR 0012 mitigation for plaintext secrets in OpenTofu state is
unblocked: pinned OpenTofu 1.11.x supports write-only attributes and the
pinned aws provider (`~> 6.43`) ships `secret_string_wo` +
`secret_string_wo_version`. Do it on the next `terraform/authentik.tf`
touch, per the ADR.

## What was already good (re-confirmed)

- Strict redirect URI matching everywhere. The one regex
  (`^http://localhost:[0-9]+/auth/callback$`, ArgoCD CLI loopback) is
  anchored and path-pinned.
- `allow_idp_initiated: false`, SHA-256 SAML digest + signature, signed SP
  keypair, 1-day temp-user cleanup on the Duo source.
- All secrets flow AWS SM → ESO → env, nothing in git. The ALB is TLS 1.3/1.2
  PQ-hybrid, HTTPS-only.
- ArgoCD: local admin disabled, default-deny RBAC (`policy.default: ""`).
- EKS OIDC `oidc:` prefixes on username and groups block
  `system:masters`-claim spoofing.
- akadmin browser path stays closed (B4), recovery is documented and needs
  kubectl exec.

## Priority for action

1. **E1** default-deny NetworkPolicy for `authentik` (then `argocd`,
   `grafana`, LGTM namespaces). Unchanged as #1 since May, and it bounds
   N5 too. eks-hardening #16.
2. **N1** disable Grafana basic auth. One values line, closes an
   internet-reachable admin path.
3. **B2 + B3** SM resource policy (ESO role + break-glass only) and a
   GetSecretValue alarm for any other principal.
4. **E3** securityContext for server/worker (+ N5, the bootstrap Job) in
   the wrapper values.
5. **#21** cert-expiry PrometheusRule. Hard wall 2027-05-06, both keypairs.
6. **N3** add `nullBytePolicy` to the ApplicationSet ignoreDifferences.
7. Plan the bundled-PG 17→18 migration and 2026.5.x chart move before
   2026.2 leaves security support, or fold it into the RDS decision.

Items 2 and 6 are one-line config changes. 1, 3, 4, 5 are small bounded
PRs. 7 is a scheduled maintenance with a datadir migration runbook.

## Docs/ADR drift fixed alongside this review

- `values.yaml` header: consumer list (Grafana/ArgoCD/Headlamp), real ADR
  filename, "bundles Postgres + Redis" claim, secret key count.
- `Chart.yaml` description: Jenkins replaced by Headlamp.
- `external-secret-config.yaml` header: all seven keys with consumers.
- ADR 0012: pod and key counts (Redis never shipped in the 2026.x chart).
- ADR 0019: dated amendment, Authentik outage now hits Grafana + ArgoCD +
  Headlamp (Jenkins claim still true).
- `authentik-bootstrap.md`: live role mapping (`percona` Viewer leg),
  ALLOWED_GROUPS, per-consumer blueprint pattern, mapping name
  (`duo-group-strip-dn`), 7-key ESO split.
- `terraform/eks-oidc-headlamp.tf`: groups claim rides the `profile` scope
  mapping (no explicit groups scope exists).
- `eks-hardening.md` #21: broadened to the two Authentik keypairs.
- `grafana-saml-cutover.md`: superseded banner. It documents the retired
  direct Grafana-SAML path (`var.grafana_saml_enabled` no longer exists,
  Grafana OSS cannot terminate SAML).

## What this review did NOT cover

Same exclusions as May: the Duo tenant and FreeIPA group hygiene,
in-app authorization beyond the entry mapping, EKS control-plane IAM
authn, and the Jenkins GitHub OAuth path (out of Authentik scope per
ADR 0019). Penetration of the running Authentik instance was not
attempted. This is a configuration and posture review.

## Verdict

No stop-the-line finding. The bridge's protocol surface is in good shape
and patched to current. The residual risk concentrates in blast radius,
exactly as in May: a flat pod network around the IdP, an all-keys secret
bundle with no read fence, one admin path that predates SSO (Grafana basic
auth), and one Duo group that is three admin roles in a trench coat. All
four have small, bounded fixes listed above.
