# Observability

The platform's observability layer is a full distributed LGTM stack
(Loki + Grafana + Tempo + Mimir) plus the Prometheus operator's
ServiceMonitor/PodMonitor reconciler and a HA Alertmanager. The
authoritative design rationale is [ADR 0010](adr/0010-distributed-lgtm.md);
this page is the operator's tour.

## Topology

```
                                                                              external pushers (Jenkins masters,
                                                                              Hetzner cloud plugin, future agents)
                                                                                            │
                                                                                            ▼
                                                                                     ALB (jenkins-cd)
                                                                              ┌─── mimir-push.cd.percona.com
                                                                              │      loki-push.cd.percona.com
                                                                              │      tempo-push.cd.percona.com
                                                                              ▼
                                          ┌───────────────────────────  alloy-gateway (Deployment x2)  ──┐
                                          │   nginx bearer sidecar :9009/:3100/:4318 (strips auth header)│
                                          │   alloy receivers loopback :19009/:13100/:14318              │
                                          │     /api/v1/metrics/write   /loki/api/v1/push   /v1/traces   │
                                          │                                                              │
                                          ▼                          ▼                                   ▼
                                         Mimir (distributed)         Loki (distributed)                Tempo (distributed)
                                         ────────────────────        ────────────────────              ────────────────────
                                         distributor x3              distributor x3                   distributor x3
                                         ingester x3 ─► S3           ingester x3 ─► S3                ingester x3 ─► S3
                                         query-frontend x2           query-frontend x2                query-frontend x2
                                         querier x2                  querier x2                       querier x2
                                         store-gateway x2            index-gateway x2                 compactor x1
                                         compactor x1                compactor x1
                                         ruler x1                    ruler x1
                                         alertmanager x3
                                          ▲
                                          │  in-cluster distributor URLs
                                          │
   alloy (DaemonSet)   -- LGTM-only since ADR 0016 (no Prom agent, no kps Alertmanager)
   ────────────────────
   ServiceMonitor / PodMonitor scrape (incl. kube-state-metrics + prometheus-node-exporter) ─► Mimir
   pod logs    ─► Loki distributor
   OTLP traces ─► Tempo distributor


   ┌───────────────────────────────────  Grafana (standalone, HA x2)  ──┐
   │                                                                    │
   │   datasources: Mimir (default), Loki, Tempo, Prometheus (in-cluster)
   │   auth: SAML/Duo (HD-30780, gated on var.grafana_saml_enabled)
   │   ALB Ingress: grafana.cd.percona.com (sticky session for SAML)
   │
   └────────────────────────────────────────────────────────────────────┘
```

## Sync waves

| Wave | Component | ArgoCD path |
|---|---|---|
| 0 | StorageClass `gp3` + AZ-pinned variants | `resources/addons/storageclass-gp3/` |
| 1 | aws-load-balancer-controller, external-dns | `resources/addons/aws-load-balancer-controller/`, `external-dns/` |
| 2 | external-secrets (with ClusterSecretStore) | `resources/addons/external-secrets/` |
| 3 | karpenter | `resources/addons/karpenter/` |
| 4 | mimir, loki, tempo | `resources/addons/{mimir,loki,tempo}/` |
| 5 | alloy DaemonSet (scrape + logs + traces), kube-state-metrics, prometheus-node-exporter, prometheus-operator-crds | `resources/addons/alloy/`, `kube-state-metrics/`, `prometheus-node-exporter/`, `prometheus-operator-crds/` |
| 6 | grafana, alloy-gateway | `resources/addons/grafana/`, `alloy-gateway/` |

Order matters: Mimir/Loki/Tempo must be running before Alloy can ship
anything. Grafana ships last so all datasources are up at first reconcile.

## Object storage

Three S3 buckets, shared CMK (`alias/percona-ci-platform-lgtm`):

| Bucket | Component | Retention | Lifecycle notes |
|---|---|---|---|
| `${cluster_name}-mimir-blocks` | Mimir TSDB blocks | 395 d (~13 mo) | Hard-delete; SSE-KMS with `bucket_key_enabled` |
| `${cluster_name}-loki-chunks`  | Loki chunks      | 30 d           | Logs decay fast; longer retention belongs in IA tier |
| `${cluster_name}-tempo-traces` | Tempo trace blocks | 14 d         | Sampling-heavy; 14 d covers incident response |

All three: BucketOwnerEnforced, public-access blocked, versioning on (for
forensics, not retention; noncurrent versions expire 7 d), abort
incomplete multipart 7 d.

IAM: each component gets `s3:Get/Put/Delete/Abort/ListMultipart` on
its own bucket plus `kms:Decrypt/Encrypt/GenerateDataKey` on the LGTM
CMK only — no cross-component access.

## Datasource URLs (in-cluster)

```
Mimir  → http://mimir-query-frontend.mimir.svc.cluster.local:8080/prometheus
Loki   → http://loki-query-frontend.loki.svc.cluster.local:3100
Tempo  → http://tempo-query-frontend.tempo.svc.cluster.local:3100
Prom   → http://prometheus-operated.monitoring.svc.cluster.local:9090   (transitional)
```

X-Scope-OrgID for Mimir is hardcoded to `percona-ci` (single-tenant); set
by Grafana datasource secureJsonData and by Prometheus remoteWrite headers.

## External push endpoints

```
mimir-push.cd.percona.com  → /api/v1/metrics/write (Prometheus remote_write
                              into Alloy's prometheus.receive_http; NOT
                              /api/v1/push, which Mimir's distributor takes
                              but Alloy's receiver does not)
loki-push.cd.percona.com   → /loki/api/v1/push
tempo-push.cd.percona.com  → /v1/traces (OTLP HTTP :4318); OTLP gRPC :4317
                              stays cluster-internal (ALB doesn't yet support
                              gRPC backend health)
```

The Alloy gateway translates these to in-cluster Service DNS calls
against Mimir/Loki/Tempo distributors.

Auth at the gateway is two intentional layers, both wired up. The active
gate is the **NGINX bearer-token sidecar** in the alloy-gateway Pod; it
validates `Authorization: Bearer <token>`, strips the header, and
proxies to Alloy's loopback receivers. The token is a single shared
value at AWS Secrets Manager
`percona-ci-platform/alloy-gateway/bearer`, synced into the cluster by
External Secrets Operator. The `SecretString` value is JSON
`{"bearer_token":"..."}` (not a plain string) — every consumer
(master-side `/usr/local/bin/alloy-fetch-token`, ESO
`dataFrom: extract`, `cdpctl verify-observability`) JSON-parses
`.bearer_token` before use. The second layer, the **ALB CIDR allowlist**
(`alb.ingress.kubernetes.io/inbound-cidrs`), is also valued: it is
empty by default in `resources/addons/alloy-gateway/values.yaml`. The
original populate-from-master-EIPs plan died with the EIP removal
(masters egress from dynamic public IPv4s); an allowlist needs a
stable-egress design first. See
[ADR 0013](adr/0013-push-from-masters-with-nginx-bearer.md) and
[`jenkins-fleet-scrape.md`](jenkins-fleet-scrape.md) for the master-side
push config.

## Memberlist isolation

Mimir, Loki, and Tempo all use Hashicorp memberlist for ring gossip and
all default to `cluster_label = ""`. Without explicit labels, three
co-tenant LGTM stacks in the same cluster (or in the same VPC) would
gossip into each other's rings on handshake, log noise at best and
cross-stack ring pollution at worst. Each stack now carries a unique
`cluster_label` (added 2026-05-08, follow-up ADR forthcoming):

```
mimir   memberlist.cluster_label = "mimir-percona-ci"
loki    memberlist.cluster_label = "loki-percona-ci"
tempo   memberlist.cluster_label = "tempo-percona-ci"
```

`cluster_label_verification_disabled: false` on all three so unlabelled
gossip from a future neighbour stack is rejected at handshake time.

## Grafana auth

Two modes, switched via `var.grafana_saml_enabled`:

- **`false` (default day-one)**: chart-generated admin password. Surface
  via `kubectl -n grafana get secret grafana -o jsonpath='{.data.admin-password}' | base64 -d`.
- **`true`**: SAML/Duo. Admin/Viewer role mapping via group attribute
  `groups`: `grafana_cd_admins → Admin`, `grafana_cd_users → Viewer`.
  Cert/key pulled from Secrets Manager via External Secrets Operator
  (path `${cluster_name}/grafana/saml/{certificate,private_key}`).

Cutover sequence: see [runbooks/grafana-saml-cutover.md](runbooks/grafana-saml-cutover.md).

## Capacity sizing (initial)

The replicas above are start-points sized for ten Jenkins masters'
typical metric/log/trace volume. Scale knobs (per-component HPA) are
deferred until usage signal arrives. Right-sizing checkpoints:

- Mimir ingester memory: bump if `cortex_ingester_memory_series` exceeds 2 M / replica
- Loki ingester memory: bump if `loki_ingester_memory_streams` exceeds 200k / replica
- Tempo ingester memory: bump if `tempo_ingester_blocks_flushed_total` rate spikes
- Prometheus WAL: bump PVC if `prometheus_remote_storage_samples_pending` accumulates

## Restore + cutover runbooks

- [Mimir restore](runbooks/restore-mimir.md) — recovering blocks from S3 versioning.
- [Grafana SAML cutover](runbooks/grafana-saml-cutover.md) — flipping on Duo SSO.

## Verifying ingest

- `just check-master-alloy` — every-active-master Mimir-via-Alloy freshness
  gate; enumerates the master set dynamically from repo source-of-truth.
- `just check-master-ingest` — per-master Mimir + Loki detail (series,
  freshness, cardinality, log lines); hardcoded master list.
- `just verify-observability [<inst>]` — full push-pipeline walk for
  one master (Alloy systemd, ALB + bearer, gateway, Mimir/Loki, Grafana).

## Related decisions

- [ADR 0010 — distributed LGTM](adr/0010-distributed-lgtm.md) (this stack)
- [ADR 0006 — kube-prometheus-stack-only](adr/0006-kube-prometheus-stack-over-mimir.md) (superseded)
- [ADR 0008 — managed NG for stateful workloads](adr/0008-managed-ng-for-stateful-system-workloads.md)
- [ADR 0009 — scrape vs remote_write for Jenkins fleet](adr/0009-scrape-vs-remote-write-for-jenkins-fleet.md)
