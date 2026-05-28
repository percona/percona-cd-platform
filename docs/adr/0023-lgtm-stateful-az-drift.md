# 0023 — LGTM stateful tier: AZ drift from ADR 0020

**Status:** Proposed (2026-05-28)
**Amends:** [ADR 0020](0020-lgtm-single-az-collapse.md) (single-AZ collapse). ADR 0020 stands; this ADR records a drift between its stated outcome and live cluster state, and the migration that closes the gap.

## Context

[ADR 0020](0020-lgtm-single-az-collapse.md) (2026-05-13) collapsed the LGTM stateful tier to single-replica, single-AZ (us-east-1a). The migration steps it describes ran cleanly for the chart values flip and the orphan PVC sweep. The AZ pinning side did not.

A live audit on 2026-05-28 found seven canonical (Bound + in-use) PVCs outside us-east-1a:

| PVC | Live PV zone | Intended zone (ADR 0020) |
|---|---|---|
| `storage-mimir-ingester-0` | us-east-1b | us-east-1a |
| `storage-mimir-store-gateway-0` | us-east-1b | us-east-1a |
| `storage-mimir-compactor-0` | us-east-1c | us-east-1a |
| `storage-mimir-alertmanager-0` | us-east-1c | us-east-1a |
| `data-loki-ingester-0` | us-east-1c | us-east-1a |
| `data-loki-compactor-0` | us-east-1c | us-east-1a |
| `data-tempo-ingester-0` | us-east-1c | us-east-1a |

### Root cause (timeline reconstructed from git + provisioning timestamps)

1. **2026-05-11** — commit `988ac6b` created the `lgtm-stateful` Karpenter NodePool with `topology.kubernetes.io/zone In: [us-east-1a, us-east-1b, us-east-1c]`. Karpenter provisioned 7 stateful-tier nodes spread across the three zones.
2. **2026-05-13 01:32 UTC** — commit `e446ff7` narrowed the NodePool requirement to `In: [us-east-1a]`. **NodePool requirement changes do not drain existing nodes.** The 5 nodes already running in 1b/1c stayed, protected from consolidation by `karpenter.sh/do-not-disrupt: "true"` on the resident pods.
3. **2026-05-13 01:38 UTC (6 minutes later)** — commit `69d2703` flattened the zone-aware StatefulSets to single replica. The new flat `-0` Pods scheduled onto whichever `lgtm-stateful` node had room; with 5 of 7 nodes in 1b/1c, that's where most of them landed. The new PVCs bound to EBS volumes in those AZs via `WaitForFirstConsumer`.
4. The pods are running today because their resident nodes survive (do-not-disrupt + the NodePool's `disruption.budgets: {nodes: "0", reasons: [Drifted, Underutilized]}`). The trap springs on any involuntary event: node-health failure, manual cordon, EKS AMI bump driving a Drift disruption, or an operator running `kubectl drain`. Once the matching-AZ node count drops to 0 on the `lgtm-stateful` pool, the resident PV cannot reattach (EBS is AZ-bound) and the pod is `Pending` with `node affinity conflict`.

### Why this hid for 15 days

- All affected pods carry `karpenter.sh/do-not-disrupt: "true"`, blocking Karpenter's voluntary disruption path.
- The `lgtm-stateful` NodePool has `expireAfter: Never` (per ADR 0020 §"Migration"), so the 30-day AMI-roll wave that catches other pools never reaches these nodes.
- The default `gp3` StorageClass has empty `allowedTopologies`, so the dynamic provisioner did not refuse the 1b/1c binding.
- `helm template` verification (ADR 0020 §"Verification") covered chart-rendered fields, not live PV AZ topology.

## Decision

Run the canonical PVC migration documented in [`lgtm-az-migration.md`](../runbooks/lgtm-az-migration.md) to relocate all seven PVCs to us-east-1a. Sequence: Mimir compactor → store-gateway → alertmanager → Tempo ingester → Loki ingester → Mimir ingester last. Each component gates on prepare-shutdown completion and object-store flush evidence before PVC delete.

Defer the `gp3` StorageClass `allowedTopologies: us-east-1a` lock to a follow-up change after all PVCs are in 1a. Locking before migration would make Bound non-1a PVCs unschedulable on any pod restart.

Keep ADR 0020's `whenScaled: Delete` PVC retention policy as-is. The procedural guardrail (prepare-shutdown + flush evidence before scale-to-0) covers the data-loss surface; flipping to `Retain` would invalidate ADR 0020's stated PVC auto-GC intent without superseding it.

## Consequences

**(+) Closes the silent reschedule trap.** After migration, any pod eviction reschedules onto a 1a node with a matching-AZ PV. ADR 0020's stated single-AZ posture becomes real.

**(+) Removes 5 stale `lgtm-stateful` nodes.** Karpenter consolidates the drained 1b/1c nodes; cluster footprint drops by ~5 r7a-class instances.

**(-) Per-stack ingest pause.** Single-member rings mean each component's slot pauses ingest for that component for the migration window (~5-15 min depending on WAL depth). Master-side Alloy queues buffer.

**(-) Mimir WAL window at risk.** Up to 2h of in-flight samples if the prepare-shutdown procedure fails to flush before PVC delete. Mitigation: the runbook gates Step 6 (PVC delete) on positive flush evidence in Step 5.

**(-) One coordinated maintenance window required.** Not a self-heal change; needs operator presence.

## Verification

Post-migration:

```sh
# All seven canonical PVCs in 1a
for ns in mimir loki tempo; do
  kubectl -n $ns get pvc -o jsonpath='{range .items[*]}{.metadata.name} {.spec.volumeName}{"\n"}{end}'
done | while read pvc pv; do
  zone=$(kubectl get pv $pv -o jsonpath='{.spec.nodeAffinity.required.nodeSelectorTerms[0].matchExpressions[?(@.key=="topology.ebs.csi.aws.com/zone")].values[0]}')
  echo "$pvc $zone"
done
# Expect: every line ends with us-east-1a

# All lgtm-stateful nodes in 1a
kubectl get nodes -l workload.percona.com/tier=lgtm-stateful \
  -o jsonpath='{range .items[*]}{.metadata.labels.topology\.kubernetes\.io/zone}{"\n"}{end}' | sort -u
# Expect: us-east-1a only

# ArgoCD apps Synced + Healthy
kubectl -n argocd get applications mimir loki tempo \
  -o custom-columns=NAME:.metadata.name,SYNC:.status.sync.status,HEALTH:.status.health.status

# Ingest path verified
./scripts/check-master-ingest.sh
./scripts/verify-observability.sh --skip-master
```

After verification, follow-up PR adds `allowedTopologies: us-east-1a` to the default `gp3` StorageClass (`resources/addons/storageclass-gp3/templates/storageclasses.yaml`). Existing Bound PVs are unaffected; the change prevents any future PVC from landing outside 1a.
