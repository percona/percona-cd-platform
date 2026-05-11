# 0017 — Cluster tier taxonomy and LGTM stateful pinning

**Status:** Accepted (2026-05-11)
**Amends:** [ADR 0008](0008-managed-ng-for-stateful-system-workloads.md) — the per-workload-taint pattern extends to LGTM stateful and the bootstrap tier.
**Related:** [ADR 0011](0011-robustness-pass.md), [ADR 0014](0014-memberlist-cluster-label-isolation.md), [ADR 0015](0015-lgtm-bucket-object-lock-removed.md), [ADR 0016](0016-lgtm-only-metrics-stack.md).

## Context

2026-05-11 14:53 CET: the two `t3.medium` nodes in the `system` EKS MNG went `NotReady`. CloudWatch showed `CPUCreditBalance = 0.0` sustained over the preceding 6 hours on both instances while `CPUUtilization` ran 21-88% (above the 40% t3.medium baseline). `StatusCheckFailed = 0` -- AWS-side host was fine. Standard credit mode throttled the instances to baseline, kubelet starved, and kubelet stopped responding to API-server health checks within the 10s window.

The MNG hosted Mimir `ingester-zone-a-0`, `ingester-zone-b-0`, `store-gateway-zone-b-0`, `compactor-0`, `alertmanager-0`, one each of querier / query-frontend / query-scheduler, plus ArgoCD / Karpenter / external-secrets / AWS LB controller. The querier's hash ring kept pointing at dead-node ingester IPs and Grafana panels returned `rpc error: server preface` on every Mimir query for ~25 minutes.

Five root causes, in order:

1. The `system` MNG was untainted, so any pod missing a `nodeSelector` could land there.
2. LGTM workloads carried no scheduling constraints whatsoever (no `nodeSelector`, no `toleration`, no `affinity`, no `topologySpreadConstraints`, no `resources.requests`). They landed on whichever node the scheduler picked.
3. `t3.medium` was sized for stateless system workloads, not for a Mimir ingester at its memory limit running a 24h Prometheus-style workload.
4. Mimir / Loki / Tempo charts ship `zoneAwareReplication.enabled=true` with `topologyKey=null`, so no `podAntiAffinity` rule is actually emitted -- three zone-aware replicas could co-locate on one host. Hardware failure on that one host took out the ring.
5. PDB shape was wrong for zone-aware StatefulSets. Each zone has 1 replica; `minAvailable: 2` on the chart's per-zone PDB meant **all** voluntary evictions were blocked forever, including the rollout-operator's coordinated zone restart.

## Decision

Define a five-tier compute topology. Each tier has a single canonical label and (where exclusive) a single canonical taint. Workloads opt in via `nodeSelector` + matching `tolerations`. The `general` tier is untainted and is the safe-fallthrough.

| Tier | Label `workload.percona.com/tier` | Taint | Capacity source |
|------|-----------------------------------|-------|------------------|
| `bootstrap` | `bootstrap` | `CriticalAddonsOnly=true:NoSchedule` | EKS MNG `system` -- `m6a.large` ×3 multi-AZ on-demand |
| `lgtm-stateful` | `lgtm-stateful` | `workload.percona.com/tier=lgtm-stateful:NoSchedule` | Karpenter NodePool `lgtm-stateful` -- on-demand `r7a/r7i/m7a/m7i` multi-AZ |
| `obs-state` | `obs-state` | `workload.percona.com/tier=obs-state:NoSchedule` | EKS MNG `prometheus_system` -- `m6a.large` ×1 us-east-1a |
| `jenkins-master` | `jenkins-master` | `workload.percona.com/tier=jenkins-master:NoSchedule` | EKS MNG `jenkins_system` -- `m6a.xlarge` ×1 us-east-1a |
| `general` | `general` | (untainted) | Karpenter NodePool `default` -- spot+on-demand `c7/m7/r7-i/a` |

DaemonSets keep the blanket `tolerations: [{operator: Exists}]` so they cover any new taint without per-DS changes.

### `CriticalAddonsOnly`, not `workload.percona.com/tier=bootstrap`

The bootstrap tier uses the AWS-canonical key documented at
https://docs.aws.amazon.com/eks/latest/userguide/critical-workload.html
and used in the EKS Blueprints `patterns/karpenter-mng/` reference. The Karpenter Helm chart, AWS LB Controller chart, and external-secrets chart all ship a default toleration for `CriticalAddonsOnly`; using the AWS-canonical key means less per-addon YAML.

### Stateful LGTM lives on its own Karpenter NodePool

`resources/addons/karpenter/templates/nodepool-lgtm-stateful.yaml`:

- `capacity-type: [on-demand]` -- spot reclamation is unacceptable for ingester WAL, store-gateway block cache, compactor scratch, AM NFLog.
- `instance-family: [r7a, r7i, m7a, m7i]` -- memory-leaning, with same-family fallback on ICE inside one pool.
- `instance-size: [large, xlarge, 2xlarge]`.
- `topology.kubernetes.io/zone In [us-east-1a, 1b, 1c]` -- zone-aware StatefulSets each find a node in their target AZ.
- `taints: [workload.percona.com/tier=lgtm-stateful:NoSchedule]` -- exclusive.
- `expireAfter: Never` -- stateful nodes do not get recycled on a clock.
- `terminationGracePeriod: 10m` -- ingester WAL flush + chunks flush headroom.
- `disruption.consolidationPolicy: WhenEmpty` (not `WhenEmptyOrUnderutilized`) -- never bin-pack a running ingester.
- `disruption.budgets`: cap `Empty` to 1 at a time; **block `Drifted` and `Underutilized` entirely** (Karpenter won't voluntarily disrupt for AMI drift or capacity rebalancing).
- `weight: 50` (> default's `10`) -- tolerating pods always land here.

EC2NodeClass `default` adds an explicit `kubelet` block (`systemReserved`, `kubeReserved`, `evictionHard`, `evictionSoft`) so memory-bound ingester nodes degrade gracefully under pressure instead of OOM-killing kubelet. Chart defaults of `memory.available: 100Mi` and zero system reserves are not adequate for an r-class node hosting a Mimir ingester at its memory limit.

### Chart-default gaps closed

These chart defaults were each load-bearing in the outage and have been fixed in the wrappers:

- **`mimir.ingester.zoneAwareReplication.topologyKey`** -- was `null`, no `podAntiAffinity` rule emitted. Set to `kubernetes.io/hostname` (chart's documented production value).
- **`mimir.store_gateway.zoneAwareReplication.topologyKey`** -- same.
- **`mimir.alertmanager.zoneAwareReplication.enabled`** -- chart default `false`; enabled with `topologyKey: kubernetes.io/hostname` to match the 3-replica HA shape.
- **`tempo.ingester.zoneAwareReplication.enabled`** -- chart default `false` ("EXPERIMENTAL" upstream); enabled because our Tempo bucket has effectively no historical data.
- **`loki.rollout_operator.enabled`** -- chart default `false`; enabled so zone-by-zone restart coordination works.
- **PDBs** -- Mimir ingester / store-gateway / alertmanager / ruler switched from `minAvailable:2` (cross-zone) to `maxUnavailable:1` (per-zone). Chart renders one PDB per zone-aware STS (1 replica each); the old shape blocked all voluntary evictions.
- **`resources.requests`/`limits`** -- chart default `{}` for every LGTM component except sidecars; set on every workload so Karpenter has scheduling signal.
- **`topologySpreadConstraints`** -- chart default `[]` for all stateless components; set on hostname `maxSkew: 1 ScheduleAnyway` so replicas don't co-locate.
- **alloy-gateway**: `topologySpreadConstraints` + soft `podAntiAffinity` on hostname; resource requests on the inner Alloy container (was unset, only nginx sidecar had explicit).

### `karpenter.sh/do-not-disrupt` on every stateful pod

Belt-and-suspenders with the NodePool's `disruption.budgets`. Stateful pods opt out of voluntary consolidation entirely; the pool already blocks `Drifted`/`Underutilized`.

### EBS volume tagging (verified, not changed)

The `gp3` family of StorageClasses (`resources/addons/storageclass-gp3/templates/storageclasses.yaml`) already declares `tagSpecification_*` parameters that propagate `iit-billing-tag=percona-ci-platform`, `PerconaKeep=True`, and `managed-by=ebs-csi-driver` onto every dynamically-provisioned EBS volume. All 26 cluster volumes verified tagged. No change needed.

## Consequences

**Cost delta:** +$220-320/mo. `system` MNG `t3.medium` ×2 (~$60/mo) -> `m6a.large` ×3 (~$135/mo); new `lgtm-stateful` Karpenter pool 2-3 × `r7a.large` (~$176-264/mo); savings of ~$30/mo from Mimir stateful no longer riding spot on the `default` NodePool. Cross-AZ data transfer for distributor->ingester fan-out is ~$2/mo at current rates -- rounding error.

**Blast radius reduction:**

- A single-node failure on the `lgtm-stateful` pool now disrupts at most one ingester zone (real `podAntiAffinity` on hostname + Karpenter's `disruption.budgets.nodes:1`); the rollout-operator coordinates the recovery zone-by-zone.
- A single-AZ failure cannot take down the bootstrap tier (3-node multi-AZ MNG); ArgoCD / Karpenter / external-secrets / AWS LB controller stay quorate.
- An AZ-1a failure still takes down Grafana / Authentik / Jenkins-ps3-k8s (PVCs are zonal on `gp3-monitoring-1a-retain` / `gp3-jenkins-1a-retain`). Multi-AZ migration of these tier-`obs-state` and tier-`jenkins-master` workloads is out of scope here and tracked as separate follow-ups.

**Operational complexity:** +1 Karpenter NodePool to maintain (`lgtm-stateful`). New tier taxonomy adds 2 labels per node (`workload.percona.com/tier`, `workload.percona.com/managed-by`); legacy `node-role`/`workload` labels stay one release for backward compat with consumer wrappers that haven't migrated to the new keys.

**Migration sequence:**

1. PR #69 (merged 2026-05-11): replaced `t3.medium` ×2 with `m6a.large` ×3 multi-AZ in the `system` MNG (the immediate outage fix).
2. PR #70 (merged 2026-05-11): additive tier labels on all MNGs + Karpenter NodePool, plus `CriticalAddonsOnly` toleration on every bootstrap-tier addon (karpenter, external-secrets, AWS LB controller, ArgoCD, external-dns, kube-state-metrics).
3. PR #71 (merged 2026-05-11): new Karpenter NodePool `lgtm-stateful`, EC2NodeClass `kubelet` block, per-workload `nodeSelector` / `tolerations` / `resources` / `topologySpreadConstraints` / `do-not-disrupt` on mimir/loki/tempo/alloy-gateway, `zoneAwareReplication.topologyKey: kubernetes.io/hostname` on Mimir ingester / store-gateway / alertmanager, Tempo zone-aware enabled, Loki `rollout_operator` enabled, Loki cache replicas `1 -> 2`.
4. Direct-to-main 2026-05-11: `CriticalAddonsOnly:NoSchedule` taint added to the `system` MNG (Stage 2b); Grafana / Authentik / Jenkins-ps3-k8s wrappers updated to `workload.percona.com/tier=obs-state` / `=jenkins-master`. Legacy `workload=prometheus` / `workload=jenkins` taints stay on the `prometheus_system` / `jenkins_system` MNGs one release alongside the new taints.

**Rollback:** every change is one `git revert` per stage. PVCs are retained, S3 data unaffected, ring state in-memory and re-elects on restart. No data-loss path.

## Out of scope (follow-up tickets)

- Grafana 2 replicas pinned to AZ-1a via `gp3-monitoring-1a-retain` PVC -- single-AZ outage = full Grafana outage.
- Authentik PG StatefulSet 1 replica AZ-1a-pinned; Authentik server 1 replica with PDB `minAvailable: 0` -- SSO SPOF.
- Jenkins-ps3-k8s master 1 replica AZ-1a-pinned, no PDB -- build SPOF. Plus the `nodeAffinity:` block in `resources/jenkins/master/instances/ps3-k8s/values.yaml` is a dangling key not consumed by the jenkins chart; the actual scheduling comes from the `gp3-jenkins-1a-retain` PVC's `allowedTopologies`. Fix is a `controller.nodeSelector` / `controller.tolerations` set in `values-base.yaml`.
- ArgoCD `application-controller-0` 1 replica, PDB `minAvailable: 0` -- GitOps SPOF.
- `kube-prometheus-stack` is uninstalled (ADR 0016) but its PVCs are retained on `gp3-monitoring-1a-retain`. Reinstall plan needs explicit decision on whether to move to multi-AZ.
- Drop the legacy `node-role=system` / `workload=prometheus` / `workload=jenkins` labels and the matching legacy taints from `terraform/eks.tf` and the Karpenter NodePool after one release once every consumer wrapper has migrated to `workload.percona.com/tier=*`.
