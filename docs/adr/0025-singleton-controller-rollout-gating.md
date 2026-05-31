# 0025 — Singleton-controller rollout gating

**Status:** Proposed (2026-05-31)
**Related:** [ADR 0024](0024-jenkins-fleet-ownership-boundary.md) (ownership boundary), [ADR 0008](0008-managed-ng-for-stateful-system-workloads.md) (managed NG for stateful workloads), [ADR 0023](0023-lgtm-stateful-az-drift.md) (the `do-not-disrupt` double-edge).

> **Implementation status: NOT YET implemented.** This ADR records the decision, not current behavior. As of 2026-05-31 the `jenkins-masters` ApplicationSet still runs `automated.selfHeal: true` + `prune: true`, `argocd-bootstrap/projects/platform.yaml` has no `syncWindows`, and there is no dedicated controller node pool, PDB, preStop quiet-down, or per-master image-digest pin. Master Applications therefore auto-sync today. The CD-A workstream implements the gating below; the Decision and Acceptance-criteria sections describe the target, not the live repo.

## Context

An OSS Jenkins controller is a singleton: the official Helm chart caps `replicas: 1`, active/active is CloudBees-only, and `JENKINS_HOME` lives on a single RWO EBS volume. Any voluntary restart (image, plugin, or LTS bump) kills in-flight builds. So a controller is a SPOF whose rollout must be deliberate, not automatic.

The dormant `ps3-k8s` scaffold proves the failure mode concretely. Its ArgoCD Application ran with `selfHeal: true`, so it kept trying to reconcile broken upstream defaults (8Gi PVC, no custom image, default agent JCasC) for 21 days. The `jenkins-masters` ApplicationSet uses a **matrix (clusters × `instances/*`) generator** with `selfHeal: true` + `prune: true`, so toggling the child Application is ineffective: the generator re-creates it. With that policy a digest-bump merge would auto-roll the singleton the moment ArgoCD reconciled, with no window and no operator present.

Two distinctions are load-bearing and routinely conflated:

1. **A 1-replica stateful controller is not a Deployment.** The standard k8s self-healing reflex (let the controller-manager and ArgoCD `selfHeal` converge state) is correct for stateless replicas and wrong for a singleton whose restart is a production outage. Argo Rollouts blue/green is also the wrong abstraction here: there is no second replica to shift traffic to.
2. **Build is not deploy.** Building a new controller image is cheap, automatic, and safe. Deploying it (bumping the digest the controller runs) is a disruptive, windowed, human-gated act. Coupling them turns every image build into an unannounced fleet restart.

## Decision

Gate every singleton-controller rollout. Disruption is operator-initiated and windowed; nothing auto-rolls a SPOF.

1. **Disable via the generator, not the child toggle.** To stop a master Application, remove or exclude its `instances/*` directory from the ApplicationSet generator glob (relocate to `_disabled/` and add a `directories` `exclude: true`). Toggling the child Application is ineffective under a `selfHeal` + `prune` matrix generator.

2. **Manual sync for master Applications.** The `jenkins-masters` ApplicationSet `syncPolicy` will drop `automated.selfHeal` and `automated.prune`. A digest-bump PR then leaves the Application `OutOfSync`; no pod rolls until an operator runs `argocd app sync`. Build stays automatic; deploy becomes an explicit act.

3. **Explicit-name sync windows, never the glob.** Deny sync windows in `argocd-bootstrap/projects/platform.yaml` will name the master Application explicitly (e.g. `jenkins-ps3-k8s`), `manualSync: true`, `timeZone: Europe/Berlin`. Never use `jenkins-*`: the bare-basename glob would freeze the live `jenkins-ingress` and `jenkins-endpoint-reconciler` addon Applications.

4. **Quiet-down + drain before any voluntary restart.** `quietDown` (stop new builds, let running finish) via a preStop hook precedes the restart. The deploy PR is human-merged, rolled ONE master at a time in an announced maintenance window, low-traffic master first.

5. **Block involuntary disruption with placement, not only `do-not-disrupt`.** A dedicated on-demand single-AZ controller node pool with a `workload.percona.com/tier=jenkins-master` taint plus a real `nodeSelector`/`tolerations` is the guard. `PDB maxUnavailable: 0` and `karpenter.sh/do-not-disrupt` block node consolidation/rolls, but per [ADR 0023](0023-lgtm-stateful-az-drift.md) `do-not-disrupt` is double-edged (it hides AZ drift and blocks planned rolls), so relax it deliberately for an intended roll. Placement (`allowedTopologies` + `nodeSelector`) is the durable guard.

Config changes (JCasC / ConfigMap clouds/templates/labels) are exempt: they reload live via the chart sidecar or `POST /configuration-as-code/reload`, no restart, and may be automatic. The exception is a config change that needs a NEW plugin, which is really an image change and inherits this gating.

## Consequences

**(+) No surprise restarts of a SPOF.** A merged image build cannot roll a controller by itself; an operator decides when, in a window, after quiet-down.

**(+) Canary stays ahead safely.** Per-master digest pins (each master its own Application) let the pilot run digest N+1 while the fleet runs N, with no auto-promotion. The reconciled plan's per-master GitOps loop depends on this.

**(+) Config velocity is preserved.** Non-disruptive JCasC reloads stay fast and can be automatic; only the disruptive image path is gated.

**(-) Deploys need operator presence.** Plugin/LTS bumps batch into an infrequent windowed cadence (e.g. monthly plus out-of-band security), not continuous delivery. This is the accepted cost of a single-controller architecture.

**(-) `selfHeal: false` weakens drift correction.** A manually-mutated master Application will not auto-repair. Mitigated by the "all changes via code" repo convention; a periodic `argocd app diff` surfaces drift without auto-applying it.

**(-) The generator-disable path is non-obvious.** An operator who toggles the child Application will see it reappear. The break-glass runbook ([ADR 0026](0026-canary-dr-operating-model.md)) documents the generator path.

## Acceptance criteria (verify once the gating is implemented)

- A digest-bump PR merged to `main` leaves the target master Application `OutOfSync` and rolls NO pod until `argocd app sync` (live check: `kubectl -n argocd get application <name> -o jsonpath='{.status.sync.status}'` reads `OutOfSync`; the pod's image is unchanged).
- The sync window in `platform.yaml` names the master Application explicitly; `jenkins-ingress` and `jenkins-endpoint-reconciler` are unaffected (their sync is not frozen).
- Relocating an `instances/*` dir out of the generator glob removes its Application; toggling the child Application does not.
- A controller pod carries the `jenkins-master` toleration and a single-AZ `nodeSelector`; `kubectl get pdb` shows `maxUnavailable: 0` on it.
- `just ci` passes.
