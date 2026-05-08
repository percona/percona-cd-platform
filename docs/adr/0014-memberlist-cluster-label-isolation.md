# 0014: Memberlist `cluster_label` isolation across LGTM stacks

**Status:** Accepted (2026-05-08)
**Related:** [ADR 0010](0010-distributed-lgtm.md) (distributed LGTM topology),
[ADR 0013](0013-push-from-masters-with-nginx-bearer.md) (the Alloy canary
that surfaced this)

## Context

Mimir, Loki, and Tempo all use the same hashicorp/memberlist gossip
implementation for ring discovery (ingesters, distributors, store-gateways,
compactors). All three components ship the same defaults out of the
upstream charts:

- `memberlist.cluster_label`: empty string (unset).
- `memberlist.cluster_label_verification_disabled`: `true`.

The three stacks land in three Kubernetes namespaces (`mimir`, `loki`,
`tempo`) on the same EKS cluster, scheduled on the same nodes, sharing the
same Pod CIDR. With no cluster_label set and verification disabled, every
memberlist node accepts every gossip packet it receives, regardless of which
LGTM stack the sender belongs to.

This was surfaced during the ps3.cd Alloy canary on 2026-05-08:

1. `mimir-push` started returning intermittent 5xx for `prometheus.scrape`
   forwards. The distributor logs cycled between
   `failed to push metric to ingester` and
   `connection refused dialing tcp <loki-pod-ip>:9095`.
2. `kubectl exec mimir-distributor-0 -- ./mimirtool ring members
   --address=...:8080 --type=ingester` listed Loki ingester pod IPs in
   Mimir's `collectors/ring`. The Mimir distributor was load-balancing
   sample writes across half-Mimir, half-Loki ingesters.
3. Loki ingester pods register `logproto.Pusher` on `:9095`. They do not
   register `cortex.Ingester`. The Mimir distributor's gRPC dial succeeded
   (the port is open), the unary call returned `Unimplemented`, and every
   PromQL query in Grafana surfaced as
   `rpc error: code = Unimplemented desc = unknown service cortex.Ingester`.

Tempo ingester pods (also on `:9095`, registering `tempopb.Pusher`) had the
same risk class but happened not to win any of the hash-ring slots Mimir
selected during the canary window. The defect is not Mimir-specific; any
cross-stack pair can corrupt any of the others' rings.

The chart defaults are wrong for any deployment that runs more than one of
the three LGTM components on the same network. Upstream documents the
toggle but does not flip it (the historical default predates running
Mimir + Loki + Tempo side-by-side as a normal pattern).

## Decision

Set a unique `memberlist.cluster_label` per stack and flip
`memberlist.cluster_label_verification_disabled` to `false` on all three.
Memberlist will then reject incoming gossip with a non-matching label at
the handshake, before the packet ever updates the ring.

| Stack | `cluster_label` |
|---|---|
| Mimir | `mimir-percona-ci` |
| Loki | `loki-percona-ci` |
| Tempo | `tempo-percona-ci` |

Touch points (one stanza per chart values file):

- `resources/addons/mimir/values.yaml`
- `resources/addons/loki/values.yaml`
- `resources/addons/tempo/values.yaml`

Each adds, under the chart's top-level `memberlist:` block (Mimir uses
`mimir.structuredConfig.memberlist`; Loki uses
`loki.loki.memberlistConfig`; Tempo uses `tempo.memberlist`):

```yaml
memberlist:
  cluster_label: <stack>-percona-ci
  cluster_label_verification_disabled: false
```

Shipped in commit `002bd6f` of `nogueiraanderson/percona-ci-platform`.

## Consequences

- **Cross-stack gossip is rejected at handshake.** The three rings stay
  scoped to their own namespace. The `cortex.Ingester / Unimplemented`
  failure mode is structurally impossible after rollout.
- **Rolling restart required per namespace.** After the values change
  syncs, every memberlist participant in each stack must restart so peers
  re-handshake under the new label. ArgoCD sync triggers this for the
  Mimir/Loki/Tempo statefulsets and deployments via the standard
  `RestartOnConfigChange` strategy.
- **Pre-existing cross-stack ring entries time out.** The poisoned ring
  entries that were observed during the canary (Loki pods inside Mimir's
  `collectors/ring`) do not need a manual purge. They expire via
  `heartbeat_timeout` (10 m default), or sooner if the ingester ring has
  `autoforget_unhealthy: true` set (Loki gets this in the same change set
  per ADR 0015).
- **Future LGTM stacks must follow the convention.** Any additional
  memberlist-using component (e.g. a second Mimir tenant, or a Pyroscope
  rollout that shares the same gossip code path) must pick a unique
  `<service>-percona-ci` label or it will be silently isolated and won't
  form a ring at all.
- **Reversibility.** Each change is a values-file edit. Rolling back to
  the upstream defaults is a one-line revert per stack — but the ring
  poisoning recurs immediately, so reverting is only useful if the LGTM
  stacks are torn down to single-stack form.

## Tracking

- Implementation commit: `002bd6f` (`nogueiraanderson/percona-ci-platform`).
- Verification: `kubectl exec mimir-distributor-0 -- wget -qO-
  http://127.0.0.1:8080/memberlist | grep '^Cluster label'` shows
  `Cluster label: mimir-percona-ci`. Equivalent check on
  `loki-distributor-0` and `tempo-distributor-0`.
- Surfaced by: ps3.cd Alloy canary, 2026-05-08 (see
  [ADR 0013 amendments](0013-push-from-masters-with-nginx-bearer.md#amendments)).
