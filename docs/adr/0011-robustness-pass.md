# 0011 — Production-readiness pass: PriorityClasses, PDBs, Object Lock, Multi-AZ NAT, VPC endpoints, native S3 state lock

**Status:** Accepted (2026-05-04)
**Related:** [ADR 0010](0010-distributed-lgtm.md), [eks-hardening.md](../eks-hardening.md)
**Superseded in part by:** [ADR 0015](0015-lgtm-bucket-object-lock-removed.md) — decision **H5** (S3 Object Lock COMPLIANCE retention on the LGTM buckets) was reversed: Object Lock broke Loki's PutObject path, so retention moved to lifecycle expiration. The bucket-level Object Lock flag remains structurally enabled but dormant (it cannot be disabled post-creation).

## Context

After the LGTM stack (ADR 0010, waves L-1 → L-5) landed, the platform's
observability shape was production-grade but the surrounding plumbing still
carried v1-PoC defaults. A self-audit against the EKS Best Practices Guide
plus the LGTM-specific failure modes revealed seven items that needed to
ship before the cluster could reasonably be called "production-ready":

- No PriorityClasses → Karpenter is free to evict any pod when consolidating.
- No PodDisruptionBudgets on the new HA components (Mimir/Loki/Tempo
  distributors, ArgoCD HA, Grafana, Alloy gateway) → a single node drain
  can take all replicas of a tier offline.
- Karpenter EC2NodeClass pinned to `al2023@latest` → silent AMI roll on
  upstream release.
- Single NAT-GW → AZ outage in the NAT's AZ kills cluster egress for all
  three private subnets.
- LGTM S3 buckets had versioning + KMS but no Object Lock → a compromised
  IAM credential or a runaway compactor could mass-delete observability
  data.
- TF state used DynamoDB for locking → extra resource to bootstrap, extra
  IAM surface, extra failure mode.
- No VPC endpoints → every ECR pull, STS Pod-Identity refresh, and
  Karpenter EC2 SDK call traversed NAT-GW egress.

Each item is small in isolation; together they're the gap between "the
cluster boots and reconciles" and "the cluster survives the kind of
incident operations actually face."

## Decision

A single robustness pass across two PRs (chart-side + TF-side) closes the
seven items above. Decisions H1–H7 below; the matching PRs are #13 (H-1)
and #14 (H-2).

### H1 — PriorityClasses (3-tier)

Three cluster-wide PriorityClasses, deployed as their own ApplicationSet
addon at sync-wave **−100** so they exist before any consumer:

| Class | Value | Used by |
|---|---|---|
| `platform-system-critical` | 1 000 000 000 | ArgoCD, AWS LBC, external-dns, ESO, EBS-CSI, Karpenter, kube-state-metrics, node-exporter, Prometheus operator, VPC-CNI |
| `platform-stateful-high` | 100 000 | Prometheus server, Alertmanager, Mimir/Loki/Tempo ingesters, Grafana |
| `platform-default` | 0 | Everything else (default; no manifest needed) |

**Why three tiers, not two**: stateful pods (ingesters, Prometheus) need
to outrank stateless API servers (queriers, distributors) so a node drain
preempts the cheap-to-restart side first. `system-cluster-critical` /
`system-node-critical` are reserved for kube-system; using them on
platform pods triggers `priorityClassName forbidden` on PSA-restricted
namespaces.

### H2 — PodDisruptionBudgets

Every HA-shaped Deployment / StatefulSet gets a PDB with
`minAvailable: replicas - 1` (or `maxUnavailable: 1` for SS). Coverage:

- ArgoCD: server, repo-server, applicationset-controller, controller
- AWS LBC, external-dns, ESO
- Mimir: distributor, ingester, querier, query-frontend, store-gateway,
  compactor, AM, ruler
- Loki: same shape
- Tempo: distributor, ingester, querier, query-frontend, compactor
- Grafana, Alloy gateway
- kube-prometheus-stack: Alertmanager, Prometheus, kube-state-metrics

**Not on**: node-exporter (DaemonSet, can't have a PDB), Alloy DaemonSet,
single-replica services (compactors are single-replica by chart default
— a PDB with `minAvailable: 1` would block all node drains).

### H3 — Karpenter AMI pin

`amiSelectorTerms.alias` flips from `al2023@latest` to a frozen
`al2023@v20260423`. Bumping is a one-line PR with deliberate review;
silent kernel/CRI/kubelet rolls don't happen.

### H4 — Multi-AZ NAT-GW

`single_nat_gateway = false`, `one_nat_gateway_per_az = true`. Each
private subnet's route table points at its own AZ's NAT — AZ outage
isolates that AZ's pods, doesn't black-hole the cluster. Cost delta:
~$32/mo idle × 2 extra NAT-GWs; data-transfer cost is dominated by S3
gateway endpoint egress (free), not NAT-GW egress.

### H5 — S3 Object Lock (Compliance, 7 d) on LGTM buckets

`object_lock_enabled = true` at bucket creation; default retention
`COMPLIANCE` mode, 7 days. Compliance mode blocks delete/overwrite even
by account root for the retention window — anti-ransomware /
anti-runaway-compactor. 7 d covers the typical incident window without
trapping legitimate operator-error cleanup indefinitely.

**Trade-off accepted**: Object Lock cannot be disabled once enabled
(creation-time only). Re-creating these buckets later costs nothing
since they're empty at the time of the change, but re-creation after
they're populated would be a careful migration.

### H6 — Native S3 state locking (no DynamoDB)

OpenTofu 1.10+ supports `use_lockfile = true` on the S3 backend, which
writes a sibling `.tflock` object next to the state file using S3
conditional writes (If-None-Match). DynamoDB lock table removed
entirely from bootstrap, IAM, and runbooks. One less resource, one
less IAM principal, one less failure mode.

### H7 — VPC endpoints (S3 gateway + 4 interface endpoints)

| Endpoint | Type | Why |
|---|---|---|
| s3 | Gateway (free) | ECR layer reads, Helm chart fetches, LGTM object I/O |
| ecr.api / ecr.dkr | Interface | Image pull is scaling-critical — every Karpenter scale-up pulls images |
| sts | Interface | Pod Identity token refresh is constant-load and latency-sensitive |
| ec2 | Interface | Karpenter EC2 SDK calls — scaling-critical |

Long-tail APIs (SQS, Secrets Manager, KMS, ELB, SSM, Logs) deliberately
deferred — low traffic, $7.30/AZ/mo per endpoint × 3 AZs adds up. Revisit
when the NAT-GW data-transfer bill warrants.

## Consequences

- **Cluster-wide pod-priority semantics**: `platform-system-critical` is now
  the load-bearing keyword. Adding a new infrastructure addon means
  picking a tier explicitly; new application workloads default to
  `platform-default` (no manifest change required).
- **Drain behaviour**: PDBs change `kubectl drain` from "evict everything
  serially" to "evict respecting minAvailable". Long-running drains on
  busy nodes are now possible — this is correct but operators need to
  know to use `--disable-eviction` only for emergencies.
- **Bucket lifecycle**: Object Lock means the LGTM buckets cannot be
  destroyed by `tofu destroy` for at least 7 days after last write. The
  cleanup-Lambda exemption tags (`iit-billing-tag`, `PerconaKeep`) handle
  the automated case; manual `tofu destroy` on a populated cluster needs
  the 7d wait.
- **Cost**: +$64/mo (2 extra NAT-GWs), +~$80/mo (4 interface endpoints
  × 3 AZs) = +$144/mo idle. NAT-GW data transfer drops by the volume of
  ECR pull + STS + EC2 API traffic — usually net cost-neutral after a
  week of normal scaling activity.
- **State migration**: `tofu init -reconfigure` after H-2 lands; the
  DynamoDB table is unmanaged after that and can be deleted manually.

## Implementation history

| Wave | PR | Scope |
|---|---|---|
| H-1 | [#13](https://github.com/Percona/percona-cd-platform/pull/13) | PriorityClasses addon, PDBs across HA components, Karpenter AMI pin |
| H-2 | [#14](https://github.com/Percona/percona-cd-platform/pull/14) | TF — Object Lock, multi-AZ NAT-GW, VPC endpoints, native S3 lock, ArgoCD priority + PDBs |
| H-3 | this PR | ADR + hardening-doc updates marking items 6/7/10/11/19/20 as Done |
