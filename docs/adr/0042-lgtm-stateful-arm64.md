# 0042 — LGTM stateful tier on arm64 (Graviton), multi-AZ to match drifted volumes

**Status:** Proposed (2026-06-27)
**Amends:** [ADR 0017](0017-cluster-tier-taxonomy-and-lgtm-pinning.md): the `lgtm-stateful` Karpenter NodePool moves from amd64 to arm64. ADR 0017 stands; this ADR records the arch change.
**Partially reverses:** [ADR 0020](0020-lgtm-single-az-collapse.md): the single-AZ (us-east-1a) pin on the stateful pool is widened back to us-east-1a/b/c. This is a pragmatic match to live state, not a return to multi-AZ HA. See the AZ-drift note below.
**Related:** [ADR 0023](0023-lgtm-stateful-az-drift.md) (the unremediated AZ drift this ADR works around), [ADR 0013](0013-push-from-masters-with-nginx-bearer.md) (the Alloy push queues that buffer the cutover pause).

## Context

The whole EKS platform cluster ran 100% amd64: all three Karpenter NodePools (`default`, `ingress`, `lgtm-stateful`) pinned `kubernetes.io/arch: [amd64]`. The `lgtm-stateful` pool backs the durable LGTM tier (Mimir / Loki / Tempo ingesters, compactors, store and index gateways, ruler, alertmanager) and ran 7 x m7i.large on-demand, about $515/mo. AWS Graviton3 is about 19% cheaper per vCPU/RAM than the equivalent Intel m7i in us-east-1 on-demand.

Two facts make the arch flip itself cheap:

1. **Every image is already multi-arch.** The LGTM workload images (`grafana/mimir:3.0.4`, `grafana/loki:3.6.7`, `grafana/tempo:2.9.0`, memcached, nginx-unprivileged, memcached-exporter) and every per-node DaemonSet image (VPC CNI, EBS and EFS CSI drivers and their sidecars, eks-pod-identity-agent, kube-proxy, grafana/alloy, node-exporter) publish a `linux/arm64` manifest at the pinned tag. Zero image rebuilds. The three single-arch internal ECR images never schedule on this pool.
2. **Storage is arch-agnostic.** A pod rescheduled onto an arm64 node in the same AZ as its volume re-binds the same PVC and re-attaches the same EBS volume, WAL preserved. Storage dollars are unchanged.

The complication is **AZ**, not arch. A pre-flight audit found the AZ drift of [ADR 0023](0023-lgtm-stateful-az-drift.md) is total and unremediated:

- All 7 stateful PVs are Bound outside us-east-1a (2 in 1b, 5 in 1c), each with hard `topology.kubernetes.io/zone` affinity to its drifted AZ.
- The NodePool hard-pins `zone: [us-east-1a]` (a tightening applied after these nodes existed). The 7 nodes are all `Drifted=True`, kept alive only by the `Drifted: 0` disruption budget.
- The gp3 StorageClass has no `allowedTopologies` lock, so the drift can recur.

Under the 1a pin, a node roll (arch flip included) would provision the replacement node only in us-east-1a, which **cannot** attach a 1b/1c volume. The drained pod would strand `Pending` on a volume-node-affinity conflict. This fragility already exists on amd64; the arch flip does not cause it, but cannot complete through these nodes while it stands.

## Decision

Two coupled changes in `resources/addons/karpenter/templates/nodepool-lgtm-stateful.yaml`:

1. **Arch to arm64.** `kubernetes.io/arch: [amd64]` to `[arm64]`; `karpenter.k8s.aws/instance-family: [r7a, r7i, m7a, m7i]` to `[r7g, m7g, r8g, m8g]` (Graviton3 primary, Graviton4 as capacity backstop). Karpenter's lowest-price logic picks `m7g.large`, mirroring today's `m7i.large`.
2. **Zone to multi-AZ.** `topology.kubernetes.io/zone: [us-east-1a]` to `[us-east-1a, us-east-1b, us-east-1c]`, matching where the volumes actually live. Each stateful pod then re-attaches its existing volume in its own AZ on a new arm64 node, so the WAL is preserved and the cutover is a brief pause rather than a rebuild.

This widening is the minimum change that lets the arch roll complete without an outage. It deliberately leaves the AZ drift in place: remediating it (migrating the 7 volumes back to 1a, then locking gp3 `allowedTopologies` and re-narrowing this pool to 1a) is the separate, pre-existing [ADR 0023](0023-lgtm-stateful-az-drift.md) project. Graviton4-only and a right-size-to-r-class consolidation were both rejected as in ADR 0017's cost framing (G4 costs more for perf we do not need; consolidation is a separate node-count decision). The `EC2NodeClass` is unchanged: the `al2023@v20260423` alias is arch-aware and resolves the arm64 AL2023 AMI (verified: `ami-0b4d0ff3bfc07e514`). On-demand, the tier taint, `expireAfter: Never`, `terminationGracePeriod: 10m`, `WhenEmpty`, the disruption budgets, and `weight: 50` are unchanged.

## Rollout

A NodePool requirement change does not drain existing nodes (the `Drifted: 0` budget blocks it). GitOps lands the spec, then the cutover is a controlled manual drain in a maintenance window.

The load-bearing mechanic is **cordon-first**. Draining one node does not force a new arm64 node: pod requests are tiny and other x86 nodes still carry the tier label, so the scheduler bin-packs the evicted pod onto an x86 sibling in the same AZ before Karpenter provisions arm64, silently no-op'ing the migration.

1. Pre-flight: confirm the arm64 AL2023 build for `v20260423` (done), Graviton on-demand offered in 1a/1b/1c (done), NodePool `limits` (64 CPU / 256Gi) headroom for the x86 + arm64 overlap.
2. Merge the spec. The `addons` ApplicationSet auto-syncs. Nodes stay `Drifted`, no pods move.
3. Cordon every x86 `lgtm-stateful` node in one sweep, before any drain.
4. Drain one component at a time, lowest-WAL-risk first: tempo, then loki, then mimir-ingester last. Karpenter provisions the replacement arm64 node in the pod's own AZ (the pod's PV affinity forces it), so the existing volume re-attaches. Let each emptied node consolidate before the next.

## Consequences

- **Cost:** about $515/mo to about $417/mo, roughly $1,177/yr (19%). Storage unchanged. Cross-AZ data-transfer for the multi-AZ pool is the ~$2/mo rounding error ADR 0017 already noted.
- **WAL preserved.** Because each pod re-attaches its own-AZ volume, the per-signal pause is about 2 to 15 minutes (flush, Graviton launch and join, EBS reattach, WAL replay), buffered within capacity by the master-side Alloy `remote_write` and `loki.write` queues (ADR 0013). An ingest pause, not loss.
- **No image work.** All workload and DaemonSet images are already multi-arch.
- **Multi-AZ is enshrined in the spec**, contradicting ADR 0020's single-AZ intent. This is accepted as a match to live state, reversible once ADR 0023 migrates the volumes back to 1a. It does not restore zone-aware HA (replication is still single-replica per ADR 0020).
- **No CI gate on family or zone names.** `just ci` does not validate these strings, so a typo surfaces as Pending pods after merge. Review the diff and watch for Pending pods post-merge.

## Verification

- `kubectl get nodes -l workload.percona.com/tier=lgtm-stateful -o wide`: all `arch=arm64`, type `m7g.large` (or a Graviton fallback). Nodes spread across 1a/1b/1c matching the volumes.
- `kubectl get pods -n mimir -n loki -n tempo`: all ingesters Running on arm64, WAL replayed, 0 restarts once settled.
- `scripts/check-master-ingest.sh`: exits non-zero if any master is missing from Mimir.
- `scripts/verify-observability.sh --skip-master`: walks Mimir and Loki distributors, canary queries, Grafana.
- Grafana shows no permanent gap in LGTM series across the cutover (a transient pause is expected).
