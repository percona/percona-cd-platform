<!-- Copyright (C) 2026 Percona LLC -->
# 0035 — EndpointSlice reconciler for EC2 master upstream discovery

**Status:** Accepted (2026-06-12)
**Related:** [ADR 0019](0019-shared-alb-ssl-termination-for-jenkins-masters.md) (the Mode B chain this plumbs; this ADR supersedes its `origin-<host>` DNS upstream mechanism for proxied masters), [ADR 0024](0024-jenkins-fleet-ownership-boundary.md) (ownership boundary the reconciler implements for EndpointSlices), [ADR 0031](0031-in-cluster-synthetic-probing-for-jenkins-masters.md) (synthetic probes observe the same path). Graduates the "Mode B plumbing" open question in [architecture.md](../architecture.md).

## Context

The Mode B ingress chain (ADR 0019) needs the in-cluster NGINX proxy to reach
each EC2 master's current private IP over the per-master VPC peering. That IP
changes on every SpotFleet replacement and AZ failover, and the masters are
EIP-less, so nothing about an instance's addressing is stable across a
rotation.

The original mechanism was DNS shaped: `terraform/origins.tf` resolved each
master's private IP at `tofu apply` time (tag-filtered `data.aws_instances`)
into an `origin-<host>.cd.percona.com` Route 53 record, and NGINX re-resolved
the name every 5 s. It converged only when an operator ran an apply, errored
mid-rotation when no instance was `running`, and published private IPs in a
public zone. That machinery has since been deleted (#228); this reconciler is
the sole upstream-discovery path.

The `jenkins-endpoint-reconciler` CronJob replaced that path with the ps3
cutover (PS-10945, PR #79, 2026-05-18) and now serves all eight proxied EC2
masters. This ADR records the decision and the alternatives, which were
previously documented only as an open question.

## Decision

A 1-minute CronJob writes per-master EndpointSlices consumed by selector-less
ClusterIP Services; NGINX upstreams point at those Services.

- **Discovery:** `ec2:DescribeInstances` per host, filtered by
  `tag:iit-billing-tag=jenkins-<inst>` and `instance-state-name=running` in
  the host's own region. The `(region, tag)` pair is distinct per master, so
  one master's query can never select another master's instance.
- **Probe-gated admission:** every candidate is probed on the Jenkins port
  before its IP is written; only a serving instance enters the slice
  (hardened to an HTTP `X-Jenkins` identity check in #215, closing the
  tagged-impostor capture class: workers and cancelled-spot-fleet ghosts
  share the master's billing tag). With multiple serving candidates the
  newest launch wins. When candidates exist but none serve, the existing
  slice is kept, never blackholed. With no running instance the slice is
  written empty and the proxy 503 page is the designed steady state.
- **Ownership:** the reconciler is the sole writer. Slices carry
  `kubernetes.io/service-name` plus the
  `endpointslice.kubernetes.io/managed-by` contract label (#209, #214) so no
  controller competes. `concurrencyPolicy: Forbid` and a deadline under the
  schedule keep writes single-flight.
- **Permissions:** EKS Pod Identity grants `ec2:DescribeInstances` only; a
  namespace-scoped Role grants EndpointSlice get/create/update only.

Convergence is bounded at roughly one minute with no human in the loop.

## Alternatives considered and rejected

- **`origin-<host>` DNS records + apply-time discovery (the predecessor).**
  Re-converged only on `tofu apply`, so every spot replacement needed an
  operator action, the apply errored during the rotation window, and private
  IPs lived in a public zone. This mechanism was retired and deleted (#228), so
  no records or operator override remain.
- **Consul service discovery.** Wrong size for the problem, which is eight
  name-to-one-IP records refreshed at 1/min. Consul means a server quorum,
  agents on masters in five regions (or WAN federation), and ACL/TLS
  lifecycle, and the consumer still needs a bridge because kube-proxy's
  native discovery primitive is exactly the EndpointSlice being written. The
  bespoke admission semantics (identity probe, newest-serving-wins,
  keep-on-ambiguous) do not map onto Consul health checks. A standing
  stateful system is a poor trade for a component scheduled for deletion.
- **Custom operator (CRD + watch loop).** EC2 exposes no watch API, so a
  controller either polls (what the CronJob already does) or grows
  EventBridge plumbing. Eight static hosts in values.yaml need no CRD. A
  singleton controller adds leader election and the rollout-gating concern
  of [ADR 0025](0025-singleton-controller-rollout-gating.md) for no
  convergence win at the accepted minute-level staleness. The CronJob gives
  per-tick failure isolation and harmless restart amnesia.
- **Stable ENI per master.** The endpoint would never change and the
  reconciler could retire. Rejected: ENIs are AZ-bound, so an AZ-failover
  replacement breaks the stable-endpoint invariant exactly when it matters,
  and each master gains ENI lifecycle Terraform.
- **Event-driven reconciliation (EventBridge instance-state events).**
  Sub-minute convergence is real but unneeded; the minute is accepted, and
  the investment trades against the in-cluster timeline. This remains the
  upgrade path if the replacement 503 window ever becomes user pain.

## Consequences

- Instance replacement converges in at most about a minute; the proxy serves
  its 503 page meanwhile. The keep-on-ambiguous rule can hold a stale IP
  while candidates exist but none pass the probe, which is deliberate
  (no-blackhole beats fast-but-wrong).
- Cross-master capture is excluded structurally (per-host tag and region
  scoping, one fixed upstream per hostname, disjoint master VPC CIDRs); the
  remaining same-master impostor class is closed by the identity probe.
- The component is transitional: each master moved in-cluster (Mode A)
  deletes its host entry, Service, slice, and peering share, and the
  reconciler retires with Mode B.
- Changes are canaryable with zero production blast radius: a one-off Job
  with a `HOSTS_JSON` subset and `TARGET_NAMESPACE` pointed at a sandbox
  namespace reproduces production behavior end to end.

## References

- Addon: `resources/addons/jenkins-endpoint-reconciler/`
- Image: `images/jenkins-endpoint-reconciler/` (`reconcile.py` carries the
  mechanism docstrings)
- Path description: [connectivity.md](../connectivity.md)
- Positioning and end state: [architecture.md](../architecture.md)
- Label contract: [Kubernetes EndpointSlice management](https://kubernetes.io/docs/concepts/services-networking/endpoint-slices/#management)
