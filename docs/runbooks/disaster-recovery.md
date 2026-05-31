# Disaster Recovery runbook

See [`../architecture.md`](../architecture.md) for context.
[ADR 0025](../adr/0025-singleton-controller-rollout-gating.md) (rollout gating)
and [ADR 0026](../adr/0026-canary-dr-operating-model.md) (operating model) define
what this runbook executes.

## Break-glass: pause Argo BEFORE any kubectl

Any emergency manual intervention on a master MUST pause ArgoCD for the affected
Application FIRST, then run `kubectl`. Otherwise selfHeal/reconcile reverts the
fix mid-incident. No step below runs a `kubectl` mutation before a sync-pause.

1. Pause the Application (master Applications are already manual-sync per
   ADR 0025; pause defensively before mutating live state):

   ```sh
   argocd app set <app> --sync-policy none
   # or: kubectl -n argocd patch application <app> --type merge \
   #       -p '{"spec":{"syncPolicy":{"automated":null}}}'
   ```

2. To FULLY stop a master Application (not just its sync): remove or relocate its
   `resources/jenkins/master/instances/<host>/` directory out of the
   `jenkins-masters` ApplicationSet generator glob. The matrix generator
   re-creates a child Application you merely toggle, so disable via the
   generator, not the child. Parked instances live in
   `resources/jenkins/_disabled/`.

3. Perform the `kubectl` intervention.

4. When done, restore sync: re-add the instance under `instances/` and run
   `argocd app sync <app>` in a maintenance window.

## Restore a master from snapshot

TODO: complete once the CSI `VolumeSnapshotClass` + snapshot controller and a
tested restore drill exist (ADR 0026 acceptance criteria). Interim shape:
restore the EBS snapshot into a new volume in the target AZ, bind a `Retain`
PVC to it, then let the manual-sync Application schedule the controller. Note
the current default PVC reclaim policy is `Delete`; the pilot must switch it to
`Retain` and AZ-pin it before it holds real data.
