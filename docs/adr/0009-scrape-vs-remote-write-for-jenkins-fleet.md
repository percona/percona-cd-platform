# 0009 — Central scrape now, agent + remote_write later (Jenkins fleet)

**Status:** Accepted (2026-04-30)

## Context

Ten Jenkins masters across five regions need to ship metrics to the central
Prometheus in `us-east-1`. Two architectures:

- **Option A — central scrape**: Prometheus on EKS pulls
  `https://<host>/prometheus` over public DNS, bearer-token auth.
- **Option B — agent + remote_write**: each Jenkins master runs Prometheus in
  agent mode and pushes to the central Prometheus' `/api/v1/write` (or to
  Mimir / Grafana Cloud later).

Option A is fastest to ship. Option B is the long-term right answer: one
outbound 443 hole per master, no NAT-GW EIP allowlist on the Jenkins side, and
the central side becomes location-independent.

## Decision

**v1 ships Option A.** Central scrape from EKS Prometheus over public DNS,
60 s interval, bearer-token auth via `additionalScrapeConfigs`. One shared
`prom-scraper` user with one bearer token mirrored across all 10 masters by
PS-10543's bootstrap groovy. Token lives in AWS Secrets Manager; External
Secrets Operator syncs it into the cluster.

**Production target is Option B.** Migration is gated on:

1. PS-10543 (prometheus plugin install + scraper user) closing.
2. PS-10996 (verify EC2 plugin metrics) closing.
3. PS-10997 (Hetzner plugin metrics in Java) closing.
4. SG tightening across all 10 Jenkins masters (drop the `0.0.0.0/0:443`
   rule).

## Consequences

- Day one: Jenkins SGs need each EKS NAT-GW EIP allowlisted on 443. Brittle.
  Acknowledged tax.
- Cross-region NAT-GW egress is metered on every scrape. Volume is small (one
  ~100 KB scrape per master per 60 s) but visible in Cost Explorer.
- Migrating to Option B is a values-file edit on the EKS side; the operational
  change is on the Jenkins-pipelines side (deploy `prometheus` agent unit on
  each master).
- The shared `prom-scraper` user model accepts one-token blast radius. Per-
  master tokens were considered and rejected — operational overhead (N rendered
  scrape jobs, N secrets) without a real security win for a read-only metrics
  endpoint. Rotation: update Secrets Manager, re-run the bootstrap groovy.
- Prometheus Agent's WAL is **not** a durable outage-survival mechanism (≈2 h
  buffer). Long central outages need a real remote backend (Mimir / Grafana
  Cloud / AMP) — that decision is downstream of this ADR.

## Update 2026-05-07: implementation diverges from original assumptions

When PS-10997 actually shipped Phase 1 (the Hetzner plugin metrics
provider), three baseline assumptions in this ADR turned out wrong, so
the v1 scrape implementation differs from the description above. Net
effect on the decision: unchanged. Net effect on the scrape config and
operational picture: documented here.

### What changed

1. **Endpoint path is `/hetzner-prometheus`, not `/prometheus`.** The
   original plan registered metrics through the Jenkins
   `metrics-plugin` extension point so the existing community
   `prometheus-plugin` would auto-export them at `/prometheus`. Fleet
   audit on 2026-05-07 found 0/10 Percona masters had
   `prometheus-plugin` installed, making that path unreachable. The
   patched plugin (`v103.percona.9`) instead bundles
   `io.prometheus:simpleclient` directly and serves a Stapler
   `RootAction` at `/hetzner-prometheus`, gated by
   `Jenkins.SYSTEM_READ`. This is a cleaner v1 with no fleet-wide
   plugin pre-req.

2. **Auth is HTTP basic_auth, not bearer.** Operationally equivalent
   for a Jenkins API token (Jenkins accepts the token as either
   `Authorization: Bearer ...` or `Authorization: Basic
   <user>:<token>`). The Prometheus `additionalScrapeConfigs` uses
   `basic_auth.password.{name,key}` referring to a Secret materialised
   by ESO from AWS Secrets Manager `percona-ci-platform/jenkins/scraper`.

3. **Connectivity is hybrid VPC peering + public+NAT-GW, not pure
   public.** The 2026-05-07 cost analysis (PrivateLink ~$73/mo break-
   even at 700 GB/mo, TGW ~$365/mo break-even at 3 TB/mo, NAT-GW
   ~$2/mo, VPC peering ~$0.26/mo) and CIDR audit showed VPC peering is
   both cheapest and aligns with private-by-default. **CIDR collision
   on `10.177.0.0/22`** between four masters (`cloud`, `ps3`, `ps57`,
   `pxc`) means only one of them can peer with the cluster's
   `10.220.0.0/16` route table at a time; the other three fall back to
   public+NAT-GW. v1 picks `ps3` as the peering slot (canary, already
   LIVE). The 3 unpeered masters keep `0.0.0.0/0:443` SG ingress until
   they are renumbered (out of scope for this phase).

4. **Route53 private hosted zone for `cd.percona.com`** in the cluster
   VPC overrides 7 master hostnames to private IPs, so the same scrape
   config (single `targets:` list) traverses peering for those 7 and
   public+NAT-GW for the other 3 transparently. The `master` relabel
   rule (regex on `__address__`) hides the path difference from
   downstream dashboards.

### What stays

- 60 s scrape interval, single shared `prom-scraper` user, central
  pull from EKS Prometheus, remote_write to Mimir.
- The Option B migration gate-conditions (PS-10543, PS-10996, PS-10997
  closing, SG tightening) are unchanged. PS-10997 closes here;
  SG tightening on the 7 peered masters happens last in this phase.

### Tracking

Implementation in commit
`bf2f68f` (`feat(addons): PS-10997 Phase 2+3 GitOps scaffolding`) and
the follow-up Terraform / SG / out-of-band steps tracked in tasks
#44-47. Plan file:
[`/home/percona/.claude/plans/spicy-prancing-nebula.md`](../../README.md).
