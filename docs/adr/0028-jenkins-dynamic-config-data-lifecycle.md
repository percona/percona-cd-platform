# 0028 — In-cluster Jenkins dynamic configuration and JENKINS_HOME data lifecycle

**Status:** Proposed (2026-06-01)
**Related:** [ADR 0024](0024-jenkins-fleet-ownership-boundary.md) (the governing principle: shrink the irreplaceable EBS residue — "plugins shift to a baked image, config to JCasC/Git, secrets to Secrets Manager"), [ADR 0027](0027-baked-jenkins-controller-image.md) (baked image / HYBRID plugins — realizes the plugin half), [ADR 0025](0025-singleton-controller-rollout-gating.md) (manual-sync gating), [ADR 0026](0026-canary-dr-operating-model.md) (single-writer cutover, DR, the snapshot/restore hard-gate), [ADR 0019](0019-shared-alb-ssl-termination-for-jenkins-masters.md) (shared ALB / host ownership).

## Context

The `ps3` master was migrated into the cluster (`ps3-k8s`) by **restoring its real EC2 `JENKINS_HOME`** onto an EBS-backed PVC and serving `ps3.cd` from the pod. That got it running, but it configured the controller by **soaking the whole restored disk**: the global `config.xml`, ~60 plugin `*.xml`, and `credentials.xml` are all on the volume and treated as the source of truth. The cost showed up immediately — the chart's default JCasC silently overwrote ps3's GitHub OAuth realm with a local admin (login broke), and the boot log is full of restored-disk drift (`CannotResolveClassException` for plugins whose dependencies aren't in the set).

[ADR 0024](0024-jenkins-fleet-ownership-boundary.md) already set the destination: a master should **converge to history-only** — rebuildable from the image (plugins), git (config), and Secrets Manager (secrets), plus a small snapshot of stateful data. [ADR 0027](0027-baked-jenkins-controller-image.md) landed the plugin half (baked image). What was missing is an explicit model for **the rest of `JENKINS_HOME`**: which config is declarative vs disk-authoritative, what is safe to remove and when, and how the volume is protected. The EBS volume must not be a black box.

## Decision

### 1. Configuration as code (the dynamic half)

System config is delivered declaratively, hot-reloaded where possible, never hand-edited on the disk:

- **JCasC** (`controller.JCasC.configScripts`, hot-reloaded by the `config-reload` sidecar): security realm, authorization strategy, global `jenkins:`/`unclassified:` settings, the Hetzner cloud, and (progressively) the credential store. The chart's default local-admin realm is disabled (`controller.JCasC.securityRealm: ""` / `authorizationStrategy: ""`), so the restored realm is never clobbered.
- **`init.groovy.d` ConfigMap** (`controller.initConfigMap`, runs on a restored home — the image ref-dir is inert on a non-empty home): the imperative EC2 cloud generation (`cloud.groovy` + `ec2FleetCloud.groovy`) that JCasC cannot express (per-AMI template loops, the runtime `ami-defs.properties` fetch). This mirrors how the EC2 masters bootstrap clouds.
- **External Secrets**: every secret a JCasC `${...}` resolves comes from AWS Secrets Manager via the platform `aws-secrets-manager` `ClusterSecretStore`, under the **`percona-ci-platform/<app>/...` naming convention** so the least-privilege ESO role (`secret:percona-ci-platform/*`) covers it with no IAM change and off-convention names are denied by default.
- **Guards:** `CASC_STRICT_SECRET_RESOLUTION=true` (a missing secret aborts the reload rather than blanking it) + a break-glass admin granted to a real GitHub login (globalMatrix has no first-user fallback).
- **DRY:** shared structure (realm/authz/cloud shapes, the byte-identical groovy, ALB annotations) belongs in `values-base.yaml`; per-instance values carry only specifics (clientID, secret refs, cloud names, subnet/AZ, the team→permission map).

Phase 1 (GitHub OAuth realm + the GitHub-team matrix authz, client secret in Secrets Manager via ESO → JCasC) is **done and verified**. Phases follow: clouds + provisioning credentials, global + plugin config, then the full credential-store migration.

### 2. The `JENKINS_HOME` data lifecycle (the volume is not a black box)

Every path in `JENKINS_HOME` (the `ps3.cd.percona.com` subPath) is classified:

- **KEEP — irreplaceable state, the volume is the source of truth:** `jobs/` (job config + `builds/` history), `fingerprints/`, `secrets/` (`master.key` + the credential-encryption keys), `users/` (GitHub user records, API tokens, preferences), `nodes/`, `userContent/`. JCasC never touches these. This is the only thing a snapshot must preserve long-term.
- **DERIVE — stays on disk as a rendered artifact, but code/image/Secrets-Manager owns it:** `config.xml` (JCasC re-renders the sections it owns on every boot — it is never deleted, just no longer authoritative), `credentials.xml` (emptied once creds move to Secrets Manager), plugin global `*.xml` (JCasC `unclassified:`), and **`plugins/`** (the baked image is the floor + forces the forks per [ADR 0027](0027-baked-jenkins-controller-image.md); the on-disk copy is a regenerable cache).
- **PRUNE — runtime/cache/orphans, safe to delete:** `queue.xml`, `logs/`, `updates/` (update-center cache), the exploded `war/`, `*.tmp`/`*.bak`, the vestigial local-admin user, and orphan plugin configs whose plugin is not installed (the `CannotResolveClassException` boot warnings).

**Removal principle — prune lags migration.** A DERIVE/PRUNE artifact is removed only **after** its replacement (JCasC/ESO/image) owns it and is verified, and **never** for the KEEP set. Each prune is a small reviewed step — a one-time root **kubectl Job** that `rm`s an explicit safe-list, or a boot-time `init.groovy.d` cleanup — never an ad-hoc broad `rm`, and always **snapshot-first**. The fenced EC2 disk is a one-time reference copy, not a backup.

**Plugins specifically:** ps3-k8s currently runs the restored `plugins/` (which is why the dependency warnings exist). Converging to image-owned plugins means **reconciling the image `plugins.txt` to ps3's actual set** (the bundler resolves the missing transitive deps), after which `plugins/` re-seeds clean and leaves the KEEP set. We do **not** set `overwritePluginsFromImage: true` (rejected in [ADR 0027](0027-baked-jenkins-controller-image.md) — it would clobber operator UI upgrades); the HYBRID floor + a complete `plugins.txt` is the path.

**End state / validation:** the volume converges to **history + secrets + users only**. The proof is a clean-seed rebuild — provision a fresh PVC seeded with *only* `jobs/` + `secrets/` + `users/`, boot it, and confirm it comes up identical; anything that was genuinely derived is reproduced from the image + JCasC + Secrets Manager.

### 3. Backups

The volume now holds the only live copy of jobs + build history, so it must be protected before any pruning relies on it:

- **In place:** the `snapshot-controller` (kube-system) + `VolumeSnapshotClass ebs-csi-retain` (`deletionPolicy: Retain`).
- **Gap:** there is **no schedule** — zero `VolumeSnapshot`s, no snapscheduler, no snapshot CronJob. The generic `percona-ci-platform-daily` AWS Backup plan does **not** cover this volume by design (it is intentionally untagged to avoid the `workload=jenkins` 14-day-vault credential leak).
- **Decision:** adopt a **scheduled `VolumeSnapshot`** mechanism (snapscheduler operator, GitOps-native, matching the ArgoCD model) with an explicit retention count, plus the **tested restore drill** that [ADR 0026](0026-canary-dr-operating-model.md) makes a hard gate (restore a snapshot into a fresh PVC, measure RTO). Cross-region copy for DR is a follow-up. The dedicated CSI schedule — not the generic AWS Backup plan — is this volume's backup.

## Consequences

- **(+)** The EBS residue shrinks toward history-only and becomes auditable; config is reviewable in git, secrets never touch the disk or the repo, and a master becomes rebuildable from code + image + a small snapshot.
- **(+)** The boot-time plugin/dep warnings resolve as `plugins.txt` is reconciled and `config.xml`/`credentials.xml` stop being hand-authored.
- **(−)** New surface to maintain: JCasC configScripts, an `init.groovy.d` ConfigMap, ESO wiring, and the `plugins.txt` reconciliation. Pruning is deliberately manual and snapshot-gated, not automatic.
- **(−)** A real prerequisite before relying on the volume: a snapshot **schedule** + a tested **restore** must land (currently a gap). Until then, treat the fenced EC2 disk as the only fallback.
