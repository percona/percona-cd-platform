# 0016 — LGTM-only metrics stack (drop kube-prometheus-stack)

**Status:** Accepted (2026-05-08)
**Supersedes (in part):** [ADR 0010](0010-distributed-lgtm.md) — keeps the Mimir / Loki / Tempo / Alloy topology decisions; supersedes the "kps stays for operator + AM + NE + KSM + ServiceMonitor reconciliation" framing.

## Context

ADR 0010 stood up the LGTM stack (Mimir + Loki + Tempo + standalone Grafana + Alloy gateway) alongside `kube-prometheus-stack` (kps). That left two parallel metrics stores running:

- **kps Prometheus** (in-cluster, 24h retention, single replica, no HA). Discovers via ServiceMonitor / PodMonitor; remote-writes everything to Mimir tenant `percona-ci`.
- **Mimir distributed** (S3-backed, multi-tenant, HA). Receives external pushes from Jenkins masters via the alloy-gateway ALB plus the kps remote-write.

Five problems with the dual-store state:

1. **Split-brain dashboards.** 28 chart-shipped sidecar dashboards bind to Grafana's `${datasource}` template variable that follows `isDefault`. The repo-owned `hetzner-plugin.json` hardcodes `"uid":"prometheus"`. Datasource UIDs were auto-derived (e.g. `PBFA97CFB590B2093`) and not pinned in YAML, so a Grafana reinstall regenerates them and breaks every committed reference.
2. **Duplicated rule evaluation.** 33 default kps PrometheusRules run in kps Prom; Mimir's ruler is enabled but unused. Recording-rule consumers tied to whichever store evaluates first.
3. **No HA story for cluster-side metrics.** kps Prom is `replicas: 1`. A pod restart drops in-flight scrapes; the local 24h PVC is the WAL. Mimir is HA but only sees what's been remote-written.
4. **Duplicated ingest cost.** kps scrapes locally and forwards anyway; the local TSDB never serves a query that couldn't go to Mimir.
5. **Two ServiceMonitor / PrometheusRule consumers.** The prometheus-operator (kps) and our future Alloy-side scraping want the same CRDs. They're co-owned uneasily.

## Decision

**Make Mimir the sole metrics store, Alloy DS the sole scraper, Mimir ruler the sole rule evaluator.** Retire `kube-prometheus-stack` entirely and replace its pieces with focused standalone charts.

This cluster is brand new. Nothing committed depends on the kps Prom URL, the kps PVC, the 33 default kps PrometheusRules, the 28 mixin sidecar dashboards, or the legacy `uid: prometheus` Grafana references. The migration lands as a **single PR** rather than the staged 8-PR plan that would be appropriate for a populated cluster.

### Component shape

| Capability | Today | After |
|---|---|---|
| ServiceMonitor / PodMonitor / Probe scrape | kps Prometheus | Alloy DS `prometheus.operator.{servicemonitors,podmonitors,probes}` with `clustering.enabled: true` |
| Recording + alert rule eval | kps Prometheus | Mimir ruler. Sync via Alloy `mimir.rules.kubernetes`. |
| Alertmanager | kps STS (3 replicas) | Mimir's built-in 3-replica AM (already running, S3-backed) |
| `monitoring.coreos.com` CRDs | kps subchart | Standalone `prometheus-operator-crds` chart (CRDs only) |
| node-exporter | kps DaemonSet | Standalone `prometheus-node-exporter` chart |
| kube-state-metrics | kps Deployment | Standalone `kube-state-metrics` chart |
| Grafana datasources | `Prometheus` (default → kps), `Mimir`, `Loki`, `Tempo` (no pinned UIDs) | Pinned `uid: mimir|loki|tempo`. `Mimir` is `isDefault: true`. No `Prometheus` entry. |
| Repo dashboard | `"uid": "prometheus"` literals | `"uid": "mimir"` literals |

### Why standalone (not in-Alloy)

Both KSM and node-exporter could run as in-Alloy components (`prometheus.exporter.kube_state_metrics` and `prometheus.exporter.unix`). We keep them as standalone charts for blast-radius isolation:

- In-Alloy KSM bakes cluster-wide read RBAC for every K8s object kind into the Alloy DS pod that also handles logs, traces, rule sync, and metrics scrape. Standalone keeps that surface scoped to one Deployment.
- In-Alloy node-exporter requires hostPID + host-mounting `/proc`, `/sys`, `/` on the same DS pod. Standalone keeps the host-priv surface scoped to one DaemonSet that does only this one thing.

## Sync ordering

ArgoCD applies all Apps from the addons ApplicationSet in parallel. When the kps Application is removed (the directory is gone), ArgoCD prunes its rendered manifests — including the `monitoring.coreos.com` CRDs (`crds.enabled: true` in kps until this PR). The standalone `prometheus-operator-crds` chart will (re)create them, but there's a window between kps's prune and the standalone reconcile where the CRDs may be absent.

For a brand-new cluster the only ServiceMonitor instances that matter today are Loki's and the new addons' own. They re-render automatically as the new addons reconcile. Worst case is a 30–120s gap where Mimir loses a few sample windows — acceptable.

The standalone chart sets `helm.sh/resource-policy: keep` on every CRD so a chart uninstall never deletes them. ArgoCD `prune: true` respects the annotation. On a populated cluster a sync wave on the standalone Application would shrink the gap further; not used here.

## Consequences

- **Single read path.** Dashboards no longer have to know which store to query.
- **HA rule evaluation.** Mimir ruler runs as a Deployment and persists rule state in S3.
- **Smaller cluster footprint.** kps had ~11 pods (1 prometheus + 3 alertmanager + 1 operator + 1 KSM + 5 NE). After: 1 KSM + 5 NE + 0 added Alloy pods (the DS already exists). Mimir AM (3 pods) was already running.
- **Stable Grafana datasource UIDs.** Pinned in `values.yaml` instead of auto-generated.
- **Two new failure domains** (KSM + NE as standalone charts) instead of bundled with kps. Net: fewer pods total, but each chart's RBAC is narrower than the kps catch-all.

## What's not preserved

The 33 default kube-prometheus-mixin PrometheusRules and the 28 sidecar K8s mixin dashboards are pruned with kps and not re-shipped. This is a brand-new cluster — nothing depended on them. If a future need arises (e.g. an SLO dashboard for the kubelet) we re-introduce only what's actually used, hand-curated, instead of carrying the full mixin set.

## Implementation references

- `resources/addons/{prometheus-operator-crds,kube-state-metrics,prometheus-node-exporter}/` — three new addon dirs.
- `resources/addons/alloy/values.yaml` — gains `mimir.rules.kubernetes "ruler"`, `prometheus.operator.{servicemonitors,podmonitors,probes}`, `prometheus.remote_write "mimir"`, `clustering.enabled: true`, raised `resources` (`requests {cpu: 200m, memory: 1Gi}, limits {memory: 2Gi}`).
- `resources/addons/grafana/values.yaml` — pinned UIDs, `Mimir.isDefault: true`, `Prometheus` datasource entry removed.
- `resources/addons/grafana/dashboards/hetzner-plugin.json` — `"uid": "prometheus"` → `"uid": "mimir"`.
- `resources/addons/mimir/values.yaml` — header comment documents the cluster's sole AM target.
- `terraform/versions.tf` — replace `kube_prometheus_stack` with three new pins.
- `scripts/verify-lgtm-cutover.sh` — phase-aware end-to-end verification.

## Open questions / follow-ups

1. **Grafana service-account token.** The verify script falls back to chart-admin basic auth and skips Grafana stages when the chart Secret is stale (Grafana hashes the initial password into its DB and ignores later env-var rotations). Provisioning a Grafana SA token via ESO would unblock the dashboard / DS-uid stages.
2. **Per-tenant Mimir model.** Today everything writes to `percona-ci`. Per-fleet tenants would enable per-tenant ingestion-rate limits.
