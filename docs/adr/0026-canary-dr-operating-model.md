# 0026 — Canary and DR operating model

**Status:** Proposed (2026-05-31)
**Related:** [ADR 0024](0024-jenkins-fleet-ownership-boundary.md) (ownership boundary), [ADR 0025](0025-singleton-controller-rollout-gating.md) (rollout gating), [ADR 0019](0019-shared-alb-ssl-termination-for-jenkins-masters.md) (shared ALB / host ownership), [ADR 0020](0020-lgtm-single-az-collapse.md) (single-AZ + WAL-on-EBS precedent), [ADR 0023](0023-lgtm-stateful-az-drift.md) (AZ-drift trap).

> **Implementation status: PARTIAL.** The break-glass ordering (pause Argo before `kubectl`) and the disable-via-generator path are now documented in `docs/runbooks/disaster-recovery.md`, and the rollout gating this builds on landed ([ADR 0025](0025-singleton-controller-rollout-gating.md), CD-A, 2026-05-31). STILL PENDING: a CSI `VolumeSnapshotClass` + snapshot controller, a `terraform/backup.tf` cross-region copy action, the seed/cutover flow, and a tested restore drill. Those sections remain target + acceptance criteria, not current state.

## Context

The fleet-management review locked a conservative posture: EC2-fronted-by-EKS stays the production default; one in-cluster controller is rebuilt correctly as a measured pilot (`ps3-k8s`, net-new beside EC2 `ps3.cd`), promoted fleet-wide only if it proves the operational model. HA is explicitly NOT a goal: a single-AZ controller with `JENKINS_HOME` on RWO EBS has the same AZ-outage exposure as one EC2 instance ([ADR 0020](0020-lgtm-single-az-collapse.md) accepted the analogous WAL-on-EBS trade-off for LGTM). What we buy is operational uniformity, GitOps, declarative lifecycle, and CSI snapshots, so the operating model must make availability expectations and recovery procedures explicit rather than implied.

Three sharp edges drove this ADR:

1. **Cutover is not just DNS.** The proxy Ingress already owns `ps3.cd` ([ADR 0019](0019-shared-alb-ssl-termination-for-jenkins-masters.md)), so a naive DNS flip creates a host-ownership conflict. external-dns's reconcile loop (≈1 min) plus the record's DNS TTL means rollback is not instant.
2. **Two writers to one `JENKINS_HOME` is data loss.** A warm EC2 rollback is only valid before the k8s controller has written; after that, re-pointing back is an RPO decision, not a free undo.
3. **Self-heal fights the operator during an incident.** Running `kubectl` against a resource ArgoCD `selfHeal`s means Argo races to revert the fix.

## Decision

Adopt one operating model covering sync cadence, single-writer cutover, single-AZ durability with a tested restore, and break-glass ordering.

### Sync windows and cadence

- Disruptive image/plugin/LTS rollouts are windowed, manual, and gated per [ADR 0025](0025-singleton-controller-rollout-gating.md): explicit-name deny sync windows, `manualSync`, `Europe/Berlin`, one master at a time, low-traffic first, quiet-down + drain before restart.
- Non-disruptive config (JCasC/ConfigMap) reloads live and may be continuous.
- `ps3-k8s` is the standing canary in both senses: the first/only in-cluster pilot, and the first master to receive any new image digest before promotion. Per-master digest pins let it run ahead of the fleet. Note the layered-coverage caveat: config changes validated on the canary are hosting-independent and de-risk EC2 masters, but the controller-image path does not exist on EC2 masters until they adopt the baked image, so image changes validated on `ps3-k8s` do NOT automatically de-risk EC2 masters.

### Single-writer cutover

Cutover treats `JENKINS_HOME` as having exactly one writer at any instant:

1. Quiet-down EC2 `ps3.cd`; let in-flight builds finish; **fence the EC2 writer** (stop the service) before the final snapshot.
2. Final EBS snapshot, reseed the AZ-pinned `Retain` PVC during a freeze window.
3. Mutate host ownership: the proxy Ingress owns `ps3.cd`, so the cutover changes/removes that Ingress host AND lets external-dns repoint the record. This is a git commit, plus the reconciler/EndpointSlice path, not just a DNS edit.
4. k8s becomes the sole writer. Keep EC2 `ps3.cd` warm (stopped, EBS retained) for rollback.
5. **Warm-EC2 rollback is valid ONLY before k8s writes.** Once the pod has written to its `JENKINS_HOME`, re-pointing to the warm EC2 is a data-loss/RPO decision, not a clean revert. external-dns's ≈1 min reconcile loop plus the record's DNS TTL means even a clean rollback is not instant.

Per the locked decisions, `ps3.cd` on EC2 is retained as the EC2 staging canary; `ps3-k8s` is net-new, not a replacement, so the "test on ps3 first" protocol survives the pilot.

> **Amendment (2026-06-01):** the pilot moved faster than this canary framing. `ps3-k8s` was cut over to serve `ps3.cd` directly from the in-cluster controller, and the EC2 NGINX proxy and endpoint-sync were retired, so `ps3.cd` now runs on Kubernetes rather than EC2. The single-writer cutover and break-glass discipline above still hold; the retained-EC2-staging-canary wording is superseded for `ps3` specifically. The other nine masters remain on EC2.

### Single-AZ + tested snapshot-restore RTO

- Accept single-AZ, single-replica (no active-active). An AZ outage pauses the controller until a replacement pod schedules and the volume restores, the documented limit, same class as one EC2.
- Durability is a CSI snapshot controller + `VolumeSnapshotClass` (`deletionPolicy: Retain`) plus a cross-region backup copy, NOT availability. A documented AWS Backup restore is an acceptable alternative path.
- A restore is real only when drilled: restore a snapshot into a fresh PVC in another AZ/namespace and **record the RTO** (consider EBS init / Fast Snapshot Restore if RTO matters). An AZ-loss drill confirms the expected `Pending`-until-restore behavior rather than discovering it during an incident. This mirrors [ADR 0023](0023-lgtm-stateful-az-drift.md): an untested AZ assumption is a silent trap until an involuntary event springs it.
- RPO floor is the snapshot cadence (the reconciled plan flags a 24h EBS-RPO floor as a decision to make explicit, not a default to inherit).

### Break-glass: pause Argo before kubectl

Any emergency manual intervention pauses ArgoCD sync for the affected Application FIRST, then runs `kubectl`. Otherwise `selfHeal`/reconcile reverts the fix mid-incident. No runbook step may place a `kubectl` mutation before a sync-pause step. The generator-disable path from [ADR 0025](0025-singleton-controller-rollout-gating.md) (not the child toggle) is the documented way to fully stop a master Application.

## Consequences

**(+) Recovery is a rehearsed procedure, not an improvisation.** RTO/RPO are measured numbers from a drill, and the AZ-loss behavior is known before it happens.

**(+) Cutover cannot silently lose builds.** Fencing the single writer and bounding warm rollback to the pre-write window makes the data-safety boundary explicit.

**(+) Break-glass ordering removes the operator-vs-Argo race.** Pausing sync first means manual fixes hold.

**(-) No HA.** A us-east-1a outage is a controller outage for the restore window. Accepted for an internal CI fleet; explicitly not suitable for customer-facing telemetry.

**(-) Rollback has a hard cutoff.** After the first k8s write, the easy warm-EC2 undo is gone; recovery becomes a snapshot-restore RPO decision. This must be communicated in the cutover window.

**(-) Windowed deploys + drill cadence need operator time.** A SPOF cannot be rolled or restored unattended; this is recurring operational cost.

**(-) The live PV is AZ-bound.** An EBS *volume* (the live `JENKINS_HOME` PV) is AZ-bound; EBS *snapshots* are regional, so a restore creates a new volume from the snapshot in the target AZ. The catch is that the running PV cannot move AZs: until the restore completes, the [ADR 0023](0023-lgtm-stateful-az-drift.md) reattach trap applies to the controller PVC. (A cross-*region* backup copy guards region loss, a separate concern.)

## Acceptance criteria (verify once implemented)

- A DR drill restores a controller snapshot into a fresh PVC in a different AZ/namespace within the recorded RTO; the restored controller boots and reconnects agents.
- An AZ-loss drill (drain the AZ node) shows the controller `Pending` until restore, confirming the known single-AZ limit rather than surprising on it.
- No runbook in `docs/runbooks/` places a `kubectl` mutation before a sync-pause step; `disaster-recovery.md` (currently a stub) must lead with pausing Argo once filled.
- A cutover dry-run confirms host ownership moves via the Ingress/external-dns/reconciler path (not a bare DNS edit) and that EC2 `ps3.cd` is fenced before the final snapshot.
- `VolumeSnapshotClass` has `deletionPolicy: Retain`; a manual snapshot of an existing PVC reaches `ReadyToUse`; a cross-region copy is observed within one cycle.
- `just ci` passes.
