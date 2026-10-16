# 0048 - In-cluster Jenkins controller change paths and their gates

**Status:** Accepted
**Amends:** [ADR 0028](0028-jenkins-dynamic-config-data-lifecycle.md) (the ps3 home has a snapshot schedule), [ADR 0025](0025-singleton-controller-rollout-gating.md) (the controller PDB is in place)

## Context

- The ps3-k8s controller changes through three paths: an image roll, a JCasC reload
  through the config-reload sidecar, and a boot-only init script change.
- The reload path had not fired since the pod started. Every configuration change
  rode a pod restart.
- ADR 0028 records no snapshot schedule for the ps3 home. snapscheduler now takes a
  daily snapshot of it.
- ADR 0025 lists the PDB as pending.

## Decision

- Each change path is proven on its own: an image roll, then config-only syncs that
  reach the running pod through the sidecar, then an init-script change.
- A voluntary controller restart requires a fresh `VolumeSnapshot` of the home, a
  Jenkins quiet-down, and zero running builds and pending provisioning.
- A reload is accepted only when the sidecar's response body carries no error, the
  controller log has no `ConfiguratorException`, the pod UID is unchanged, and the
  changed value reads back from the live object. `lastTimeLoaded` alone is not proof,
  because the plugin sets it before parsing.
- The controller carries a PDB with `maxUnavailable: 0`. Node-group label or taint
  changes follow `docs/runbooks/mng-label-taint-changes.md`.
- The JCasC token-reload endpoint is denied at the ALB. Only the in-pod sidecar uses
  it, on localhost.
- The image ships no init scripts. Boot Groovy is limited to what JCasC cannot
  express and lives in the controller values.

## Consequences

- A failed reload is attributable to its sync, and a restart is never the only proof
  that configuration applies.
- The daily snapshot is the recovery floor, a fresh one is the recovery point for a
  planned change.
- Draining the controller node needs an explicit step, by design.
