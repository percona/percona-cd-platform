# 0016 — LGTM-only metrics stack (drop kube-prometheus-stack)

**Status:** Accepted (2026-05-08)
**Supersedes (in part):** [ADR 0010](0010-distributed-lgtm.md) — keeps L-1..L-12 about Mimir / Loki / Tempo / Alloy topology, supersedes the "kps stays for operator + AM + NE + KSM + ServiceMonitor reconciliation" framing.

## Context

ADR 0010 stood up Mimir + Loki + Tempo + standalone Grafana + Alloy gateway alongside `kube-prometheus-stack`. That left two parallel metrics stores running:

- **kps Prometheus** (in-cluster, 24h retention, single replica, no HA). Discovers via ServiceMonitor / PodMonitor; remote-writes everything to Mimir tenant `percona-ci`.
- **Mimir distributed** (S3-backed, multi-tenant, HA). Receives external pushes from Jenkins masters via the alloy-gateway ALB and the kps remote-write.

Five problems with the dual-store state:

1. **Split-brain dashboards.** 28 chart-shipped sidecar dashboards bind to Grafana's `${datasource}` template variable that follows `isDefault`. The repo-owned `hetzner-plugin.json` hardcodes `"uid":"prometheus"`. Datasource UIDs were auto-derived (`PBFA97CFB590B2093`) and not persisted in the YAML, so a Grafana reinstall regenerates them and breaks every committed reference. Plus 4 file-provisioned dashboards (gnetId fetches), 2 of which fetched the wrong dashboard ID and are unusable (`mimir-overview` 19279 → "test", `tempo-operational` 16886 → "Traefik 2").
2. **Duplicated rule evaluation.** 33 default kps PrometheusRules run in kps Prom; Mimir's ruler is enabled but unused. Recording-rule consumers (12 of 28 K8s mixin dashboards) are tied to whichever store evaluates first.
3. **No HA story for cluster-side metrics.** kps Prom is `replicas: 1`. A pod restart drops in-flight scrapes; the local 24h PVC is the WAL. Mimir is HA but only sees what's been remote-written.
4. **Duplicated ingest cost.** kps scrapes locally and forwards anyway; the local TSDB never serves a query that couldn't go to Mimir.
5. **Two ServiceMonitor / PodMonitor / PrometheusRule consumers.** The prometheus-operator (kps) is one. Future Alloy-side scraping wants the same CRDs. Today they're co-owned uneasily.

ps3.cd canary verified end-to-end on 2026-05-08: `hetzner_*` from master-side Alloy lands in Mimir within 16s, Loki carries the master log stream with full ring health (3/3 ingesters ACTIVE). kps Prom remote-writes 6945 samples/s into Mimir steadily. The transitional dual-store wasn't broken — it was redundant.

## Decision

**Make Mimir the sole metrics store, Alloy DS the sole scraper, Mimir ruler the sole rule evaluator.** Retire `kube-prometheus-stack` entirely and replace its pieces with focused standalone charts.

### Concrete component shape (D1..D7)

| Capability | Today | After |
|---|---|---|
| ServiceMonitor / PodMonitor / Probe scrape | kps Prometheus | Alloy DS `prometheus.operator.{servicemonitors,podmonitors,probes}` with `clustering.enabled: true` |
| Recording + alert rule eval | kps Prometheus | Mimir ruler. Sync via Alloy `mimir.rules.kubernetes` (PrometheusRule CRD → Mimir HTTP API). |
| Alertmanager | kps STS (3 replicas) | Mimir's built-in 3-replica AM (already running, S3-backed) |
| `monitoring.coreos.com` CRDs | kps subchart | Standalone `prometheus-operator-crds` chart (CRDs only, no operator pod) |
| node-exporter | kps DaemonSet | Standalone `prometheus-node-exporter` chart (kept separate from Alloy DS to scope the hostPID/host-mount blast radius) |
| kube-state-metrics | kps Deployment | Standalone `kube-state-metrics` chart (kept separate from Alloy DS to scope cluster-wide read RBAC) |
| Grafana datasources | `Prometheus` (default → kps), `Mimir`, `Loki`, `Tempo` (no pinned UIDs) | Pinned `uid: mimir|loki|tempo|prometheus`. `Mimir` is `isDefault: true`. `Prometheus` aliased to Mimir's URL until D7 retires every legacy dashboard ref. |
| Repo dashboard | `"uid": "prometheus"` literals | `"uid": "mimir"` literals |

### Migration sequence (P1..P6)

1. **P1** — Pin Grafana datasource UIDs; clean dead `kube-prometheus-stack.grafana.ingress.hosts` block. Behavior change: zero.
2. **P2** — Install standalone `prometheus-operator-crds` (additive). CRDs become co-owned with kps under server-side apply; the standalone chart sets `helm.sh/resource-policy: keep` so the kps prune in P6 doesn't take them down.
3. **P3** — Land `mimir.rules.kubernetes` in Alloy DS. Mimir ruler starts evaluating the same PrometheusRules in parallel with kps. Identical sample timestamps → ingester dedupes. Also declares the `prometheus.remote_write "mimir"` block that P4 will use.
4. **P4** — Add `prometheus.operator.{servicemonitors,podmonitors,probes}` to Alloy DS with `clustering.enabled: true`. Opens the parallel-scrape window: kps Prom AND Alloy DS both push. `external_labels.cluster = "percona-ci-platform"` lets ops verify both writers active before cutting over.
5. **P5a** — Flip Grafana `isDefault` from Prometheus to Mimir. Repoint the `Prometheus` datasource URL to Mimir's query frontend (alias). Migrate `hetzner-plugin.json` literals.
6. **P5b** — `prometheus.replicas: 0` on kps Prom. Disable kps `kubeStateMetrics` and `nodeExporter`. Install standalone KSM and node-exporter charts (their ServiceMonitors are picked up by Alloy from P4).
7. **P6** — Manual: annotate the 10 `monitoring.coreos.com` CRDs `argocd.argoproj.io/sync-options: Prune=false`. Then delete `resources/addons/kube-prometheus-stack/` entirely. Document Mimir AM as the cluster's sole alert router.

Each phase is reversible without restoring backups. Verification at every phase via `scripts/verify-lgtm-cutover.sh --phase {pre|ruler|post|worker|dashboard|failsafe}`.

### What we lose

- The 33 default kps PrometheusRules (kube-prometheus mixins) get pruned with kps in P6. The Mimir ruler stops evaluating them on the next Alloy sync (~1 minute). 12 of the 28 sidecar dashboards depend on these recording rules; 16 don't. If we want to keep the 33, copy the manifests to a new `prometheus-rules` addon before P6 lands. Tracked as a follow-up.
- The 28 chart-shipped sidecar dashboards are pruned with kps. JSONs are well-known (kube-prometheus mixins on GitHub) — easy to re-ship as a `resources/addons/lgtm-dashboards/` addon if/when needed.

### What we keep that didn't fit anywhere else

- The standalone `Prometheus` datasource entry, aliased to Mimir's query frontend. Dashboards that hardcode `"uid": "prometheus"` keep working without a JSON edit. Once every dashboard is migrated to `"uid": "mimir"`, the alias entry can be retired (a future P6 follow-up).
- The kps Prometheus PVC (gp3-monitoring-1a-retain). Not deleted by P5b's `replicas: 0` — survives so a rollback (`replicas: 1`) reads the WAL within seconds. P6 deletes it via chart removal.

## Consequences

- **Single read path** for everything. Dashboards no longer have to know which store to query.
- **HA rule evaluation.** Mimir ruler runs as a Deployment and persists rule state in S3.
- **Same scrape coverage.** Alloy DS clustering shards ServiceMonitors across the 5 DS pods (today: ~13 SM targets total — KSM, NE, Loki, future Alloy-shipped). Resource footprint goes from kps's ~600Mi cluster-wide (1 prometheus + 3 alertmanager + 1 operator + 1 KSM + 5 NE) to ~5Gi for Alloy DS resource bumps + ~50Mi each for KSM and NE — net comparable, with HA built in.
- **One CRD owner long-term.** The standalone `prometheus-operator-crds` chart. The kps subchart's CRDs are kept around as long as kps stays via SSA; P6 makes the standalone chart the sole owner.
- **Grafana datasource UIDs are now stable across reinstalls.** Pinned in `values.yaml` instead of auto-generated.
- **Two new failure domains** (KSM + NE as standalone charts) instead of bundled with kps. Net: fewer pods total, but each chart's RBAC is narrower than the kps catch-all, which we wanted.

## Implementation references

- `resources/addons/{prometheus-operator-crds,kube-state-metrics,prometheus-node-exporter}/` — the three new addon dirs.
- `resources/addons/alloy/values.yaml` — gains `mimir.rules.kubernetes "ruler"` (P3), `prometheus.operator.{servicemonitors,podmonitors,probes}` (P4), `prometheus.remote_write "mimir"` (P3+), `clustering.enabled: true`, raised `resources` (`requests {cpu: 200m, memory: 1Gi}, limits {memory: 2Gi}`).
- `resources/addons/grafana/values.yaml` — pinned UIDs (P1), default flip + `Prometheus` aliased URL (P5a).
- `resources/addons/grafana/dashboards/hetzner-plugin.json` — `"uid": "prometheus"` → `"uid": "mimir"` (P5a).
- `resources/addons/mimir/values.yaml` — header comment documents `mimir-alertmanager.mimir.svc.cluster.local:8080` as the cluster's sole AM target (P6).
- `terraform/versions.tf` — `prometheus_operator_crds`, `kube_state_metrics`, `prometheus_node_exporter` pins; `kube_prometheus_stack` pin removed in P6.
- `scripts/verify-lgtm-cutover.sh` — phase-aware verification.

## Open questions / follow-ups

1. **Recording-rule preservation.** Decide whether to ship a `prometheus-rules` addon mirroring the kps mixin set before P6, or accept the loss of the 12 mixin-dependent dashboards.
2. **Aliased `Prometheus` datasource lifetime.** Drop the alias entry once D7 has flipped every committed dashboard literal to `uid: mimir`.
3. **`disable_login_form: true` interaction with API basic auth.** The chart-rotated admin password in the Grafana Secret didn't authenticate against the live DB on this cluster (Grafana hashes the initial password into its DB; later env-var rotations are ignored without `force_reset_admin_password`). The verify-lgtm-cutover.sh skips Grafana stages when basic auth fails. Long-term, provision a Grafana service-account token via ESO and consume it as `GRAFANA_TOKEN` in CI.
4. **Mimir tenant model.** Today everything writes to `percona-ci`. Per-fleet tenants (one per Jenkins master domain, plus `cluster` for in-cluster scrape) would let us bound blast radius via per-tenant ingestion-rate limits. Out of scope for ADR 0016.
