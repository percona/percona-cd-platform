# 0018 — LGTM single-AZ, single-replica collapse

**Status:** Accepted (2026-05-13)
**Supersedes:** Multi-AZ ingester topology described in [ADR 0010](0010-distributed-lgtm.md) and the multi-AZ portion of [ADR 0017](0017-cluster-tier-taxonomy-and-lgtm-pinning.md).
**Related:** [ADR 0014](0014-memberlist-cluster-label-isolation.md), [ADR 0015](0015-lgtm-bucket-object-lock-removed.md).

## Context

[ADR 0017](0017-cluster-tier-taxonomy-and-lgtm-pinning.md) (2026-05-11) pinned the LGTM stateful tier to a Karpenter NodePool spanning us-east-1{a,b,c} so that the chart-default `zoneAwareReplication.enabled: true` could land one ingester replica per zone with `podAntiAffinity` via the rollout-operator. That topology was correct, but on an internal CI observability stack the durability story does not warrant its cost:

1. **Durability lives in S3, not in EBS.** Mimir blocks (`percona-ci-platform-mimir-blocks`), Loki chunks (`percona-ci-platform-loki-chunks`), and Tempo traces (`percona-ci-platform-tempo-traces`) are the long-term store. The ingester WAL on EBS only buffers ~2h (Mimir) / ~5min (Loki) / ~30s (Tempo) of in-flight samples before flush. An AZ outage costs at most that window per stack; the rest is on S3 and recoverable.
2. **Multi-AZ NAT cost dominates.** Three NAT gateways (one per AZ) at $32/month + per-GB processing dwarf the EBS savings on a few-hundred-GB observability footprint. Collapsing to a single NAT in us-east-1a saves ~$200/month.
3. **Zone-aware HA without rollout-operator coordination is illusory.** The chart's per-zone PDBs (`minAvailable: 2` on a single-replica zone STS) blocked all voluntary evictions, including the rollout-operator's own coordinated zone restart (see ADR 0017 root-cause #5). Functional HA required the rollout-operator pipeline plus a careful sequencing dance the team did not need.
4. **Memberlist with `cluster_label_verification_disabled: true` (ADR 0014) is unforgiving of cross-stack pod restarts.** Fewer ingester pods is fewer surface area for ring/cluster_label drift.

## Decision

Collapse all LGTM stateful components to **single-replica, single-AZ (us-east-1a)**:

| Component | Replicas | Storage |
|---|---:|---|
| Mimir ingester | 1 | 50 GiB gp3, WAL only |
| Mimir store-gateway | 1 | 30 GiB gp3, block-list cache |
| Mimir compactor | 1 | 30 GiB gp3, scratch |
| Mimir alertmanager | 1 | 5 GiB gp3, silences + NFLog |
| Loki ingester | 1 | 50 GiB gp3, WAL only |
| Loki compactor | 1 | 30 GiB gp3, scratch |
| Tempo ingester | 1 | 30 GiB gp3, WAL only |

All other Loki distributed modes (`singleBinary`, `read`, `write`, `backend`) pinned to `replicas: 0`. The `rollout_operator` chart is disabled in both Mimir and Loki since no zone-aware StatefulSets remain for it to coordinate.

`replication_factor: 1` in both Mimir and Loki (per-component `commonConfig.replication_factor` for Loki; implicit from `zoneAwareReplication.enabled: false` for Mimir). The ingester ring becomes single-member; writes go to one ingester and either succeed or block until S3 flush.

### Storage model

EBS is treated as a regenerable WAL. S3 is the source of truth. The `gp3` StorageClass uses `reclaimPolicy: Delete`; combined with `persistentVolumeClaimRetentionPolicy: {whenDeleted: Delete, whenScaled: Delete}` on every stateful component, **PVCs auto-GC** with the StatefulSet. This closes the leak class observed on 2026-05-13 where ~445 GiB of zone-aware EBS volumes were orphaned by the topology flip and required a manual sweep ([runbook](../runbooks/lgtm-orphan-pvc-sweep.md)).

The chart keys that turn this on:

- Mimir (per component: `ingester`, `store_gateway`, `compactor`, `alertmanager`): `<c>.persistentVolume.enableRetentionPolicy: true` + `whenDeleted`/`whenScaled: Delete`
- Loki (per component: `ingester`, `compactor`): `<c>.persistence.enableStatefulSetAutoDeletePVC: true` + `whenDeleted`/`whenScaled: Delete`
- Tempo (per component: `ingester`): `<c>.persistentVolumeClaimRetentionPolicy.enabled: true` + `whenDeleted`/`whenScaled: Delete`

## Consequences

**(+) Cost.** ~$200/month savings (single NAT + retired idle EBS). Idle EBS budget alone was ~$36/month for the orphaned volumes.

**(+) Operational simplicity.** No rollout-operator coordination dance. Pod restart = single STS rolling restart. The ring is trivially observable (`/ingester/ring` has one member).

**(+) PVC hygiene.** Future scale-downs and topology flips self-clean. No more `kubectl get pvc -A | grep zone` archaeology.

**(-) AZ outage.** A us-east-1a outage pauses ingest until pods reschedule (Karpenter must provision a replacement node in a different AZ; ingester restart may require WAL replay from S3 if dirty). Query path is unaffected for any data already in S3.

**(-) Sub-block-flush data loss is possible during an instance failure.** WAL on EBS is per-instance; if the EBS volume is lost (rare but possible), up to one flush window is lost. Acceptable for an internal CI obs stack; not acceptable for prod customer telemetry.

**(-) No memberlist HA.** Single ingester = single ring member = no failover during pod rolling restart. Ingest pauses for the restart window (~30s). Same trade-off as any single-replica STS.

## Migration

The flip happened in commit `69d2703` (2026-05-13). Migration steps applied:

1. `prepare-shutdown` -> `flush` -> `shutdown` on each zone-{b,c} ingester (Mimir and Loki) before scaling them to 0. Ensures the WAL was flushed to S3 before zone replicas were removed.
2. Helm value change to `zoneAwareReplication.enabled: false`, `replicas: 1`, single-AZ nodeSelector.
3. ArgoCD reconciled, pruning the zone-aware StatefulSets via `PruneLast=true` (the new flat STS came up healthy before the old zone STSs were torn down).
4. **(this change)** Add `persistentVolumeClaimRetentionPolicy` to values so future flips do not leak. Sweep the 17 orphan PVCs from the migration via the [runbook](../runbooks/lgtm-orphan-pvc-sweep.md). Sweep the Released Grafana PV manually since `gp3-monitoring-1a-retain` blocks the cleanup Lambda.

## Verification

- `scripts/check-master-ingest.sh` shows all 10 Jenkins masters reporting at <60s freshness in Mimir and active Loki line counts. Re-run after the next pod restart to confirm WAL replay from S3 works.
- `scripts/verify-observability.sh --skip-master` walks the full ALB -> alloy-gateway -> Mimir/Loki/Tempo pipeline.
- `helm template resources/addons/{mimir,loki,tempo}/` shows `persistentVolumeClaimRetentionPolicy: {whenDeleted: Delete, whenScaled: Delete}` on every stateful STS.
- `kubectl get pvc -A | grep -E 'zone-[abc]|tempo-ingester-[12]|kafka-data-mimir'` is empty.
- `kubectl get pv | awk '$5=="Released"'` is empty.
