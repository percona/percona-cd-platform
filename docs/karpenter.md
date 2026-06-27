# Karpenter

How the cluster gets and sheds compute. Karpenter serves the two elastic
tiers of the [compute topology](architecture.md#compute-topology-eks-cell),
while the three stateful tiers stay on managed node groups by design. Tier
taxonomy and its origin story:
[ADR 0017](adr/0017-cluster-tier-taxonomy-and-lgtm-pinning.md). AZ strategy:
[ADR 0020](adr/0020-lgtm-single-az-collapse.md).

## Install and prerequisites

- Chart `karpenter` 1.12.0 from `oci://public.ecr.aws/karpenter`, pinned in
  `terraform/versions.tf`, consumed as the single dependency of the
  `resources/addons/karpenter/` umbrella chart and synced by the addons
  ApplicationSet. CRDs ship inside the chart and apply via
  `ServerSideApply=true`.
- Prerequisites live in `terraform/karpenter-prereqs.tf` (the upstream EKS
  karpenter submodule): the controller role with its Pod Identity
  association, the stable node role `percona-ci-platform-karpenter-node`
  (no name prefix, so the EC2NodeClass reference never churns), and the
  SQS interruption queue for spot termination and rebalance events.
- The controller policy is attached inline because the Karpenter v1 policy
  exceeds the 6,144-character managed-policy quota. The trade-off: it does
  not appear in `aws iam list-policies`.
- Discovery is tag-driven: private subnets and both cluster security groups
  carry `karpenter.sh/discovery = percona-ci-platform`. A missing security
  group tag means no node ever provisions while everything else looks
  healthy, the controller just logs a selector mismatch.
- The controller runs 2 replicas on the `bootstrap` tier with
  `platform-system-critical` priority, so Karpenter never depends on the
  capacity it manages.

## NodePools

Upstream reference: [NodePools](https://karpenter.sh/docs/concepts/nodepools/)
and [disruption](https://karpenter.sh/docs/concepts/disruption/).

| | `default` | `lgtm-stateful` | `ingress` |
|---|---|---|---|
| Tier label | `general` (untainted fallthrough) | `lgtm-stateful` (exclusive NoSchedule taint) | `ingress` (exclusive NoSchedule taint) |
| Capacity | spot first, on-demand fallback | on-demand only | spot first, on-demand fallback |
| Arch | amd64 | arm64 (Graviton) | amd64 |
| Families | c7i, c7a, m7i, m7a, r7i, r7a | r7g, m7g, r8g, m8g | t3a/t3 medium, c7a/c7i/m7a/m7i large (instance-type list) |
| Sizes | large to 4xlarge | large to 2xlarge | medium, large |
| AZ | us-east-1a | us-east-1a/b/c (matches drifted volumes, ADR 0023) | us-east-1a/b/c (multi-AZ) |
| Expiry | 720h (30-day roll for AMI freshness) | Never, 10 min termination grace | 720h |
| Disruption | `WhenEmptyOrUnderutilized`, consolidate after 1 m, budget 1 node | `WhenEmpty` only, drift and underutilized budgets blocked at 0 | `WhenEmpty`, consolidate after 5 m, budget 1 node |
| Limits | 200 CPU, 800 Gi | 64 CPU, 256 Gi | 8 CPU, 32 Gi |
| Weight | 10 | 50 | 60 |

The shape encodes two lessons. Stateless work tolerates spot and
consolidation, so the `default` pool chases price, with the explicit 1-node
disruption budget protecting single-replica stateless LGTM components from
co-eviction. Ingesters do not tolerate either, so `lgtm-stateful` is
on-demand, never bin-packed while running, and never expired, a direct
consequence of the 2026-05-11 CPU-credit outage that created the taxonomy.

The `default` pool pins us-east-1a: EBS is zonal, the elastic tier carries no
durable state, and keeping it in 1a lets consolidation fully vacate the other
AZs (the single-NAT collapse rides the same decision). The `lgtm-stateful` pool
spanned 1a only by intent (ADR 0020), but its stateful volumes drifted into
1b/1c (ADR 0023, still open) and the pods follow their per-AZ EBS, so the pool
now lists all three AZs to match reality (a 1a-only pin would strand the drifted
pods on any node roll). It re-narrows to 1a once the volumes migrate back. The
`ingress` pool is multi-AZ by design: it spans all
three AZs so the jenkins-ingress NGINX web plane can place one replica per node
across distinct AZs (the proxy carries no EBS, so the zonal-data argument does
not apply). It is tainted so the single-AZ tenants above cannot drift onto it
and follow the proxy across AZs (ADR 0019 de-pin amendment, 2026-06-15).

## EC2NodeClass

Upstream reference: [NodeClasses](https://karpenter.sh/docs/concepts/nodeclasses/).
One `default` class shared by both pools:

- AMI alias `al2023@v20260423`, the EKS-optimized AL2023 build for the
  current cluster version, bumped during the
  [eks-upgrade runbook](runbooks/eks-upgrade.md) sweep.
- Subnet and SG selection by the discovery tag, so adding capacity surface
  is a one-tag change.
- IMDSv2 required with hop limit 1: pods cannot reach the node's IMDS,
  making Pod Identity the only credential path
  ([`pod-identity.md`](pod-identity.md)).
- Kubelet reservations are absolute bytes, not percentages (percentages
  drift with instance size), with hard eviction at 500 Mi.
- Every launched instance and volume re-asserts `iit-billing-tag` and
  `PerconaKeep: "True"`. These are mandatory: without them the account
  cleanup reapers terminate nodes within minutes and delete volumes daily
  ([`runbooks/cleanup-reapers.md`](runbooks/cleanup-reapers.md)).

## Operational notes

- Metrics: the chart's ServiceMonitor is enabled in our values (upstream
  default is off). Alloy auto-discovers it. Karpenter emits many series
  lazily, so NodePool and NodeClaim metrics appear only after the first
  provisioning or disruption event.
- The interruption queue name reaches the chart from the cluster-secret
  annotation. Before that overlay existed, the chart used a placeholder
  name and spot-interruption handling was silently disabled while
  provisioning kept working, worth remembering as a failure mode.
- Smoke tests: `scripts/verify-karpenter.sh` with fixtures under
  `scripts/karpenter-tests/` (burst, spread, PDB, do-not-disrupt, limits).
