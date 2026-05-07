# 0012 — Authentik as Duo SAML → OIDC bridge

**Status:** Accepted (2026-05-07)
**Amends:** [ADR 0010](0010-distributed-lgtm.md) (the "Auth at the edge"
section there assumed Grafana would speak SAML to Duo directly; that path
turned out to be Enterprise-only, which this ADR resolves).

## Context

Percona's internal IdP is **Duo SSO** (FreeIPA-backed, owned by IT-Ops).
Internal apps that need SSO must federate to Duo via SAML 2.0 — that is the
only protocol IT-Ops will register an SP record for, and the SP record is
the gating artifact (cert, ACS URL, entityID).

ADR 0010 planned for Grafana to speak SAML to Duo directly via
`GF_AUTH_SAML_*` env vars. Two facts surfaced after PR #23 landed:

1. **Grafana SAML is Grafana Enterprise-only.** The OSS image has no SAML
   handler — `/saml/metadata` logs `handler=notfound` and the login page
   never renders the "Sign in with Duo" button. Confirmed against Grafana
   13.0.5 and the upstream source. Buying Enterprise for one signal type
   has poor ROI; we don't need any other Enterprise feature.
2. **Other near-term consumers have the same shape.** Jenkins masters,
   ArgoCD UI, and Authentik's own admin UI each speak OIDC natively but
   either don't ship SAML (ArgoCD), ship a SAML mode that doesn't match
   Duo's SP-record convention (Jenkins SAML plugin), or only speak OIDC
   (Authentik). A per-app SAML solution would multiply the SP-record
   coordination burden with IT-Ops by N apps.

We need a single component that talks SAML to Duo once and exposes OIDC
inwards to every consumer.

We initially scoped **Authelia** for this role. Two parallel research
agents confirmed via the official Authelia roadmap and docs that Authelia
**does not implement SAML 2.0 in any form** — neither IdP nor SP. The
SAML 2.0 IdP roadmap entry has no shipped code or timeline. Its
first-factor backends are LDAP/file only; there is no "delegate to
external SAML IdP" mode.

## Decision

Run a single **Authentik** instance as a SAML-to-OIDC bridge. Authentik
(MIT-licensed, single Helm chart) ships native SAML SP + OIDC IdP today
plus group attribute mapping between the two — exactly the shape the
bridge requires.

Each consuming app talks plain OIDC to Authentik at `auth.cd.percona.com`.
Authentik handles the SAML round-trip with Duo on the user's behalf and
propagates group memberships from Duo's FreeIPA LDAP groups into the
OIDC `groups` claim, so each app's role mapping reads bare CN names
(`grafana_cd_admins`, `percona`) and not full DN strings.

Operational detail and full request flow live in
[`docs/authentication.md`](../authentication.md). This ADR captures only
the "why this shape" decisions.

### Sub-decisions

**A1 — Authentik over alternatives.**

| Option | Rejected because |
|---|---|
| Grafana Enterprise (SAML direct) | Enterprise license cost for one feature; doesn't generalize to Jenkins / ArgoCD |
| Authelia | No SAML 2.0 support (SP or IdP); roadmap entry only |
| Keycloak | Heavier (JVM + separate DB profile required from day one); larger ops surface than the bridge needs |
| DIY SAML proxy (e.g. saml2aws-style) | Hand-rolling auth code is the worst place to take on maintenance |
| Per-app SAML (one SP record per consumer) | Multiplies IT-Ops coordination by N; each cert rotation = N tickets |

**A2 — Single Authentik replica with bundled Postgres/Redis.**

Bundled Postgres uses RWO PVCs on `gp3-monitoring-1a-retain`. Horizontal
scale needs RDS first (covered in "Out of scope"). Single replica is
acceptable today because:

- `priorityClassName: platform-system-critical` keeps Karpenter
  consolidation from preempting it.
- Already-issued OIDC sessions survive Authentik downtime; only new
  logins block.
- Each consumer with a local-admin emergency path keeps it open until the
  bridge has demonstrated reliability over multiple weeks.

**A3 — Codified bootstrap (blueprint ConfigMap + PostSync Job), not
manual UI clicks.**

The first cut of this work used a runbook-driven UI configuration (SAML
source + OAuth2 provider + application created by hand). After Authentik
worker restarts dropped state we couldn't easily reconcile, the entire
bootstrap was moved to an Authentik **blueprint YAML** mounted via
ConfigMap and applied by a PostSync Job hitting the Authentik API. Worker
restarts now re-reconcile cleanly and the bootstrap is reviewable in git.

**A4 — Group rename via `group_property_mappings`, not user mapping.**

Duo sends groups as full FreeIPA LDAP DNs:

```
cn=grafana_cd_admins,cn=groups,cn=accounts,dc=int,dc=percona,dc=com
```

Authentik 2024.8 split SAML source mapping into two distinct fields:
`user_property_mappings` (runs once on the assertion, populates User
fields) and `group_property_mappings` (runs once **per** `group_id`,
returns `{"name": "<new_name>"}` which becomes the Authentik
`Group.name`). Stripping the DN to a bare CN lives in
`group_property_mappings: [duo-group-strip-dn]`. Doing it under
`user_property_mappings` would silently fail because the `groups` key
returned there is merged with append-unique semantics onto a base
properties dict — the user keeps both the renamed bare CN *and* the
original DN. Discovered the hard way; documented in
[`docs/authentication.md`](../authentication.md#group-propagation).

**A5 — Per-app role mapping in the consumer, not in Authentik.**

Each consuming app applies its own role rules against the OIDC `groups`
claim. Grafana uses `GF_AUTH_GENERIC_OAUTH_ROLE_ATTRIBUTE_PATH` with a
JMESPath expression; Jenkins/ArgoCD will use their respective OIDC plugin
group-to-role configs. Authentik stays role-agnostic; it just signals
group membership. Adding a new role tier per app does not require an
Authentik change.

**A6 — `disable_login_form: true` on consumers once SSO is validated.**

Each consumer keeps its local-admin login form on during the soak window
(emergency path during Authentik bring-up). After a working SSO round
trip is confirmed, the form is disabled. Recovery moves to in-pod
`kubectl exec` for the local-admin, documented in
[`docs/authentication.md`](../authentication.md#recovery-paths).

## Consequences

- **Operational surface**: one new namespace (`authentik`), four new
  pods (server + worker + bundled Postgres + bundled Redis), one new
  ALB Ingress rule (joins existing `jenkins-cd` group, no new LB), one
  new Secrets Manager bundle with four random_password keys.
- **SP record management is centralized.** One SP record at Duo, one
  cert pair, one ACS URL. SP cert rotation = one HD JSM Change Request,
  not N.
- **Group sync gap on existing users.** The `default-source-authentication`
  flow has no User Write stage, so re-login does not re-sync group
  memberships for users who already exist in Authentik. Workaround:
  delete the Authentik user via API; their next login goes through
  `default-source-enrollment` which does run user_write +
  GroupUpdateStage. Long-term: define a custom auth flow with user_write,
  or rely on Duo group churn being slow enough to handle manually.
- **Single point of auth failure.** Authentik down = no new logins for
  any consumer. Mitigation: each consumer keeps an in-pod local-admin
  recovery path (Grafana `grafana-cli admin reset-admin-password`,
  ArgoCD admin Secret, Jenkins script console). `priorityClassName:
  platform-system-critical` blocks Karpenter consolidation.
- **akadmin local-login is closed at the browser surface** as of 2026-05-07
  (`user_fields: []` on the identification stage). With one registered
  source, the frontend auto-redirects to Duo without rendering an
  Authentik form — closing the previous Duo-MFA bypass via the local
  `akadmin` user. Recovery is via `ak create_recovery_key <minutes>
  akadmin` in the worker pod (Authentik's official troubleshooting
  procedure); see [runbook](../runbooks/authentik-bootstrap.md#lockout-recovery--akadmin).
  This raises the bar for "use akadmin to bypass Duo" from "anyone with
  the password" to "anyone with kubectl exec on the cluster."
- **No automation for Duo SP record changes.** Cert rotation, ACS URL
  changes, and group attribute mappings all require an HD JSM ticket or
  IT-Ops Slack. Tracked separately under hardening item #21 (PrometheusRule
  on cert expiry, calendar reminder, runbook).
- **Reversibility.** The bridge is one ApplicationSet entry + one TF file.
  Removing it means removing the OIDC env block in each consumer and
  pointing them at local-admin login (or, if Grafana Enterprise becomes
  affordable later, switching consumers back to direct SAML one at a
  time).

## Implementation history

| Wave | PR / commit | Scope |
|---|---|---|
| A-1 | [#26](https://github.com/nogueiraanderson/percona-ci-platform/pull/26) | TF bootstrap: random secrets + Secrets Manager bundle + var/data/local rename from `grafana_saml_*` to `authentik_*` |
| A-2 | [#27](https://github.com/nogueiraanderson/percona-ci-platform/pull/27) | Authentik chart wrapper + ApplicationSet entry + bootstrap runbook |
| A-2b | [#29](https://github.com/nogueiraanderson/percona-ci-platform/pull/29) | Codify SAML source + Grafana OIDC client as blueprint ConfigMap + PostSync Job (replaces manual UI runbook) |
| A-3 | `952c753` (direct to main) | Grafana SAML→OIDC cutover: drop SAML env block + cert mounts, add OIDC env block, ALLOWED_GROUPS gate, role mapping |
| A-3b | `55f4164` | Move LDAP DN strip from `user_property_mappings` to `group_property_mappings` (Authentik 2024.8 split) |
| A-3c | `684095d` | Disable Grafana local login form after SSO validation |

Operational reference: [`docs/authentication.md`](../authentication.md).
Recovery procedures: [`docs/authentication.md#recovery-paths`](../authentication.md#recovery-paths).

## Out of scope

- **Postgres → RDS migration.** Bundled chart Postgres is fine for v1;
  move when we add HA replicas or other Authentik consumers push load
  past the single instance.
- **Authentik replicas: 2+.** Depends on RDS migration above.
- **Jenkins masters via Authentik OIDC.** Same chart-wrapper pattern,
  separate PR per master pair.
- **ArgoCD UI via Authentik OIDC.** Replaces ArgoCD's bundled Dex; one
  `argocd-cm` config change.
- **Custom auth flow with User Write stage.** Closes the group-sync gap
  for existing users without the delete-and-re-enroll dance.
- **SAML SP cert expiry alerting.** Hardening item #21 — PrometheusRule +
  calendar reminder + runbook.
