# 0008 — Managed NodeGroups for stateful system workloads

**Status:** Accepted (2026-04-30)
**Amended by:** [ADR 0017](0017-cluster-tier-taxonomy-and-lgtm-pinning.md) (per-workload taints became the `workload.percona.com/tier` taxonomy). The Decision table below is updated to the current live topology; see the history-and-drift note under it.

## Context

Three classes of pods need stable scheduling decisions independent of
Karpenter's spot-default consolidation:

- **kube-system + Karpenter controller + ArgoCD** — chicken-and-egg: Karpenter
  cannot schedule its own controller.
- **Prometheus / Alertmanager / Grafana** — stateful, gp3 EBS is zonal,
  Karpenter consolidation would churn the node and the pod would re-bind every
  time. "Stable, on-demand, not interrupted by spot."
- **Jenkins masters** — same zonality + churn-aversion as Prometheus, plus
  per-master sizing knobs and independent rolling-update cadence.

## Decision

Three EKS **managed NodeGroups**:

| NG | Capacity | AZ | Taint (`key=value:effect`) | Tier label `workload.percona.com/tier` | Hosts |
|---|---|---|---|---|---|
| `system` | on-demand `m6a.large × 3` | multi-AZ | `CriticalAddonsOnly=true:NoSchedule` | `bootstrap` | kube-system, Karpenter controller, ArgoCD, AWS LB controller, external-dns, EBS-CSI controller, external-secrets, kube-state-metrics, CoreDNS |
| `prometheus_system` | on-demand `m6a.large × 1` | `us-east-1a` only | `workload.percona.com/tier=obs-state:NoSchedule` | `obs-state` | Grafana, Authentik (server + bundled PG), MTR CloudNativePG |
| `jenkins_master` | on-demand `m6a.xlarge × 1` | `us-east-1a` only | `workload.percona.com/tier=jenkins-master:NoSchedule` | `jenkins-master` | in-cluster Jenkins controller pilot (`ps3-k8s`) |

Karpenter's `default` NodePool must `NotIn` the bootstrap/stateful taints (`CriticalAddonsOnly` and `workload.percona.com/tier`) per the taxonomy in [ADR 0017](0017-cluster-tier-taxonomy-and-lgtm-pinning.md). LGTM stateful pods (Mimir/Loki/Tempo) run on a dedicated Karpenter `lgtm-stateful` NodePool, not a managed NG.

> **History + live drift (as of 2026-05-31).** The original `system` `t3.medium × 2` was replaced by `m6a.large × 3` (multi-AZ) after the 2026-05-11 CPU-credit outage (ADR 0017). The original `jenkins-system` NG (`m6a.xlarge`, `workload=jenkins`) was **removed 2026-05-13** — no Jenkins pod ever claimed its taint. The `jenkins_master` NG above was **recreated 2026-05-31** for the singleton-controller pilot ([ADR 0025](0025-singleton-controller-rollout-gating.md)) but is **live-but-uncommitted**: it is not yet in `terraform/eks.tf` (which still carries the 2026-05-13 removal comment), so a `tofu apply` from committed code would destroy it. Committing the block closes the drift.

## Consequences

- Each stateful workload class has its own lifecycle: rolling-update one NG
  doesn't disturb the others. NG drain blast radius is one workload.
- AZ pinning (us-east-1a) for prometheus_system and jenkins_master is an
  accepted SPOF for v1 — gp3 is zonal, so multi-AZ HA needs EFS (slower for
  fsync-heavy Jenkins) or a leader-election pattern (overkill). See
  `docs/observability.md` and `docs/runbooks/restore-prometheus.md` for the AZ-
  outage recovery plan.
- Karpenter's spot fleet still serves everything else (proxy NGINX, future
  workloads). Costs stay bounded.
- Rejected: a Karpenter `system` NodePool. Premature abstraction — the managed
  NG already covers controller bootstrap.
