# 0010 — Full distributed LGTM observability stack

**Status:** Accepted (2026-05-06)
**Supersedes:** [ADR 0006](0006-kube-prometheus-stack-over-mimir.md)

## Context

ADR 0006 chose `kube-prometheus-stack` as a single-chart observability stack
"at v1" with LGTM (Loki / Grafana / Tempo / Mimir) deferred. After waves 1–5
the platform is no longer "v1 / PoC" — it's the durable home for ten Jenkins
masters in five regions and any future shared platform service. Observability
needs to land in its production shape from the start.

Three constraints shaped the new design:

1. **Multi-source pushers**. The 9 Jenkins EC2 masters (with prometheus-plugin)
   and the patched Hetzner cloud plugin will push metrics, logs, and traces
   into the cluster from outside the VPC. The platform must accept external
   pushes without exposing Mimir/Loki/Tempo distributors directly to the
   internet.
2. **Long retention for metrics**. CI failure correlation across releases
   needs ≥13 months of metric history — Prometheus' local TSDB doesn't fit.
3. **Logs + traces have day-one consumers**. Jenkins build console output to
   Loki; pipeline-step OTLP traces to Tempo. CloudWatch (the v1 fallback) is
   too clunky for cross-instance build forensics.

## Decision

Replace the kube-prometheus-stack monoculture with a full distributed LGTM
deployment, bridged to the Prometheus operator that stays for AM + NE + KSM
+ ServiceMonitor reconciliation. Twelve sub-decisions (L1–L12) drive the
shape.

### Topology

| Component | Mode | Replicas (initial) | Storage |
|---|---|---|---|
| Mimir | distributed | 3 distrib + 3 ing + 2 querier + 2 store-gw + 1 compactor + 3 AM + 1 ruler + 2 qf + 2 qs | S3 + per-component WAL on `gp3` |
| Loki | `deploymentMode: Distributed` | 3 distrib + 3 ing + 2 querier + 2 qf + 2 qs + 1 compactor + 2 idx-gw + 1 ruler | S3 + WAL on `gp3` |
| Tempo | distributed | 3 distrib + 3 ing + 2 querier + 2 qf + 1 compactor | S3 + WAL on `gp3` |
| Grafana | standalone | 2 (HA, sticky-session ALB) | EBS `gp3-monitoring-1a-retain` |
| Alloy DaemonSet | per-node | 1 per node | none |
| Alloy gateway | Deployment | 2 | none |
| Prometheus (kps) | server, agent-shaped | 1 | EBS WAL only (10 Gi) |
| Alertmanager (kps) | HA via operator | 3, anti-affinity | EBS `gp3-monitoring-1a-retain` |

### Storage backends

Three S3 buckets (`${cluster_name}-{mimir-blocks,loki-chunks,tempo-traces}`)
backed by a shared KMS CMK (`${cluster_name}-lgtm`), separate from the
cluster CMK and the AWS Backup CMK. Per-component lifecycle: Mimir 395 d,
Loki 30 d, Tempo 14 d. Public-access blocked, versioning on, BucketOwnerEnforced.

### IAM

Pod Identity per component, scoped to its own bucket only. No IRSA,
no SA annotations. Hand-rolled `aws_iam_policy_document` per component
(S3 bucket-scoped + LGTM CMK only).

### Auth at the edge

- **Internal**: Grafana datasources query Mimir/Loki/Tempo by Service DNS
  inside the cluster, no auth (single-tenant, in-VPC).
- **External pushers**: Alloy gateway behind ALB at `mimir-push`,
  `loki-push`, `tempo-push` under `cd.percona.com`. v1 auth = ALB CIDR
  allowlist (operator-set). Bearer-token enforcement via NGINX sidecar is
  the next hardening item.
- **Grafana SSO**: SAML/Duo via [HD-30780](https://perconadev.atlassian.net/browse/HD-30780),
  group → role mapping `grafana_cd_admins → Admin`,
  `grafana_cd_users → Viewer`. Gated by `var.grafana_saml_enabled`; the
  cluster boots on local-admin until IT Ops returns SP metadata + populates
  Secrets Manager. Cutover runbook: [runbooks/grafana-saml-cutover.md](../runbooks/grafana-saml-cutover.md).

### What stays from kube-prometheus-stack

Prometheus operator + CRDs (every ServiceMonitor / PodMonitor /
PrometheusRule cluster-wide flows through it), Alertmanager, node-exporter,
kube-state-metrics. Bundled Grafana off — too narrow a values surface for
the SAML group mapping.

Prometheus is now agent-shaped: 24 h retention, 8 GiB retentionSize, 10 Gi
PVC. It's the WAL while remoteWrite to Mimir is in-flight or catching up.
Sample-loss bound on a Mimir outage = Prometheus WAL replay window.

### Why distributed mode (not monolithic)

User-selected. Each component has independent scale knobs (e.g. Mimir
ingester replicas vs querier replicas), so the cluster can grow into one
dimension without re-architecting the others. Operationally heavier
than monolithic mode but matches the "production-grade from day one"
intent.

### Why Alloy, not Prometheus + Promtail + OTel Collector

Alloy is the supported successor to Grafana Agent + Promtail + OTel
Collector. Single binary, single configuration file (River), three signal
types — replaces three separate DaemonSets that would otherwise fight for
node resources.

Metrics scraping is **not** moved to Alloy at this time. Prometheus
operator-managed scrape (ServiceMonitor / PodMonitor) is the cluster-wide
standard; running Alloy + Prometheus in parallel for the same job is
duplicative.

## Consequences

- **Operational complexity**: ~30+ new pods. Each LGTM component has its
  own scale tuning, retention curves, and S3 bucket.
- **Cost**: three new S3 buckets (small at v1; lifecycle bounds growth),
  one new KMS CMK, three new Pod Identity associations.
- **Networking**: three new ALB Ingress rules at `mimir-push`,
  `loki-push`, `tempo-push`. They share the existing `jenkins-cd` ALB
  group, no new load balancer.
- **Backup**: AWS Backup keeps covering Prometheus + Jenkins + Alertmanager
  EBS volumes via the existing `workload=*` selection. Mimir/Loki/Tempo
  ingester WALs live on the cluster-default `gp3` (multi-AZ, Delete) —
  not backed up; durable data is in S3.
- **Reversibility**: each LGTM component is its own ApplicationSet entry.
  Decommissioning one (e.g. Tempo, if traces never materialize) is one
  directory delete. Mimir is the hardest to back out of since metrics
  data flows through it; the path back to local Prometheus storage is
  removing remoteWrite + restoring retention/PVC size.

## Implementation history

| Wave | PR | Scope |
|---|---|---|
| L-1 | [#8](https://github.com/nogueiraanderson/percona-ci-platform/pull/8) | S3 buckets + KMS + Pod Identity |
| L-2 | [#9](https://github.com/nogueiraanderson/percona-ci-platform/pull/9) | Mimir/Loki/Tempo Helm wrappers |
| L-3 | [#10](https://github.com/nogueiraanderson/percona-ci-platform/pull/10) | kube-prometheus-stack agent + AM HA |
| L-4 | [#11](https://github.com/nogueiraanderson/percona-ci-platform/pull/11) | Grafana + Alloy + Alloy gateway + SAML |
| L-5 | this PR | ADR + observability.md + runbooks |
