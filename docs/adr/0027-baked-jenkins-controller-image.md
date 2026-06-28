# 0027 — Baked Jenkins controller image (ECR + GHA-OIDC push), HYBRID plugin reconciliation

**Status:** Accepted (2026-05-31); in-cluster consumption is the Stage 0 pilot (see [ADR 0025](0025-singleton-controller-rollout-gating.md)).
**Amended by:** [ADR 0043](0043-platform-wide-arm64-migration.md): jenkins-percona is now multi-arch, pinned by the multi-arch index digest (Decision item 2's amd64-only base pin is superseded).
**Related:** [ADR 0024](0024-jenkins-fleet-ownership-boundary.md) (this realizes its "plugins shift to a baked image" governing principle — shrinking the irreplaceable EBS residue), [ADR 0025](0025-singleton-controller-rollout-gating.md) (the gated in-cluster pilot consumes this image), [ADR 0021](0021-mtr-test-history-relational-store.md) (sibling "pinned ECR image + EKS workload, built via scoped GHA-OIDC" pattern).

## Context

[ADR 0024](0024-jenkins-fleet-ownership-boundary.md) set the governing principle for the fleet: keep the irreplaceable EBS residue as small as possible — *"plugins shift to a baked image, config to JCasC/Git, secrets to Secrets Manager"* — so a master converges to history-only and becomes rebuildable from code plus a small snapshot. 0024 explicitly flagged the gap: *"Plugins still live on EBS until the baked image lands."* This ADR records the decision that lands it.

The EC2 fleet installs plugins at boot. That leaves three problems for the in-cluster pilot (and for EC2 reproducibility): (1) plugins are part of the irreplaceable `$JENKINS_HOME` residue, not code; (2) there is no provenance or determinism — a boot-time resolve can drift; (3) the two **patched Percona plugin forks** (EC2 cloud, Hetzner cloud — canonical in `Percona-Lab/jenkins-{ec2,hetzner-cloud}-plugin`) must be guaranteed to win over whatever an imported real-EC2 `$JENKINS_HOME` PVC already carries, or the master runs an unpatched cloud plugin.

A baked, immutable, provenance-carrying controller image solves all three, but only if (a) its build cannot be tampered with, (b) it does not require committing binary HPIs to git, and (c) its plugin-reconciliation policy is precise about what it forces versus what it merely floors — so a human's UI plugin upgrade on a live master is not silently clobbered while the patched forks always win.

## Decision

Build a controller image `percona-cd/jenkins-percona` from `images/jenkins/` and consume it from the in-cluster Jenkins chart. Six load-bearing choices:

1. **Bake upstream LTS + community plugins + the two patched forks.** Base `jenkins/jenkins:2.541.3-lts-jdk17` (matches the live EC2 fleet), community set in `plugins.txt` (pinned), forks fetched at build time. The image replaces runtime plugin install for the in-cluster path.

2. **Pin the base by per-platform amd64 digest, not the multi-arch index tag.** `FROM …@sha256:<amd64-digest>` so an upstream re-push of the same tag cannot silently change our base. amd64-only (EKS nodes are amd64; the arm64 m3 build host buildx cross-builds). Digest is refreshed (and the `Resolved <DATE>:` comment updated) on every LTS bump.

3. **Fork HPIs are SHA-verified at fetch, never committed.** `fetch-hpis.sh` reads `percona-plugins.lock.json` (`id`/`version`/`filename`/`url`/`sha256`), downloads each public `Percona-Lab/*` release asset, verifies `sha256` before staging it as `<id>.jpi`, and hard-fails on any mismatch or non-hex digest. No GitHub token needed; no binaries in git (`percona-plugins/` holds only `.gitkeep`, HPIs are gitignored).

4. **HYBRID plugin reconciliation — forks FORCED, community SOFT.** The chart sets `installPlugins: false` and `overwritePluginsFromImage: false` (inert when `installPlugins: false`). The Dockerfile renames **only** the two fork plugins to `<id>.jpi.override` (the ref-dir seeder force-installs `.override` files unconditionally on every boot, so the patched fork wins even over a populated PVC-restored home). Community plugins stay plain `<id>.jpi`, which the seeder version-compares and seeds only when the home lacks the plugin or carries an older build — so the image is the **floor** for community plugins (a UI upgrade persists) and the **source of truth** for the forks.

5. **Build and push via GitHub-Actions OIDC, push only from `main`.** `.github/workflows/build-jenkins-image.yml`: the `validate` job (pull requests) builds + smoke-boots with **no** AWS token and **no** push; the `publish` job (push to `main`) is the **only** job with `id-token: write`, assumes the scoped role in `terraform/iam-gha-jenkins-image-push.tf`, pushes with SBOM + provenance, captures the pushed digest, and re-smokes the pushed `@sha256:` digest. Third-party actions are SHA-pinned. The ECR repo is `IMMUTABLE` + `scan_on_push`, with an untagged-only lifecycle (`terraform/ecr.tf`) that never expires a deployed (tagged) digest.

6. **Chart references the image by an immutable SHA-TAG `<lts>-<gitsha>`, not a true `@sha256:` digest — today.** The upstream `jenkins` subchart (5.9.18) has no `digest` key, so a real digest pin waits on the pilot chart rebuild. ECR immutability + a unique SHA-tag get most of the immutability benefit; bump PRs must be honest that this is a **TAG bump, not a digest bump** (the build records the pushed digest in the PR body).

The in-cluster consumption (the chart consuming `jenkins:`-scoped values) is the **Stage 0 pilot** gated by [ADR 0025](0025-singleton-controller-rollout-gating.md); the EC2 fleet continues to install plugins at boot until/if it adopts the image.

## Consequences

**(+) Realizes ADR 0024's residue-shrink.** Plugins move from the EBS `$JENKINS_HOME` residue into versioned, rebuildable code — the prerequisite for a snapshot-backed, rebuildable master.

**(+) Provenance + determinism.** SHA-verified fork fetch, SBOM + provenance attestations on push, a committed `plugins.effective.txt` audit of the fully-resolved set, and a `smoke-boot.sh` that asserts each fork loads at its locked version (and re-smokes the pushed digest) make "what's in this image" auditable.

**(+) Patched forks always win, even on a byte-identical EC2 restore.** The `.override` mechanism preserves the "restore a real EC2 `$JENKINS_HOME` and still get the patched cloud plugins" property without the manual, error-prone snapshot-stripping alternative.

**(+) Tamper-resistant supply chain.** PR builds hold no AWS credentials; only push-to-`main` mints the OIDC JWT; the push role is scoped to this one repo; the ECR repo is immutable.

**(−) TAG, not a true digest pin, today.** Until the subchart gains a `digest` key, a determined re-tag is only prevented by ECR immutability, not by the chart contract. Honest gap; tracked.

**(−) Community plugins are floored, not pinned.** A UI-installed/upgraded community plugin on a live master persists across restarts unless the image pins a newer version — deliberate (don't clobber operators), but it means the image is not the sole source of truth for the community set.

**(−) New build + OIDC trust surface to maintain.** A workflow, a scoped IAM role, a base digest, and a plugin lock all need upkeep; the restored-home smoke fixture is not yet wired into CI (follow-up).

## Alternatives considered

- **Runtime plugin install (status quo on EC2).** Keeps plugins in the irreplaceable EBS residue, no provenance, drifts at boot. Rejected for the in-cluster path; it is exactly what ADR 0024 wants to retire.
- **`overwritePluginsFromImage: true`.** Forces the *whole* baked set, defeating the soft-community half (would clobber operator UI upgrades). Rejected.
- **Strip plugins from the seed snapshot.** A one-time, manual, error-prone step that must be redone on every new snapshot, is invisible in code review, and loses the byte-identical-to-EC2 restore property. Rejected.
- **Commit fork HPIs into git.** Repo bloat + goes stale; a SHA-verified fetch from the public Percona-Lab releases is leaner and provable. Rejected.

## Verification

- `aws ecr describe-repositories` shows `percona-cd/jenkins-percona` is `IMMUTABLE` with `scanOnPush`; its lifecycle expires **untagged-only** (never a deployed digest) — `terraform/ecr.tf`.
- The `validate` job declares `contents: read` with **no** `id-token`; only `publish` has `id-token: write` and runs on `push` to `main`.
- `fetch-hpis.sh` hard-fails on a `sha256` mismatch or a non-hex digest; HPIs are absent from git (`percona-plugins/` is `.gitkeep` + gitignored binaries).
- `smoke-boot.sh` asserts each baked fork loads at its locked version on an empty home, and (with `SMOKE_RESTORED_HOME_TAR`) over a populated `$JENKINS_HOME` to prove the `.override` path.
- `just ci` passes (fmt, validate, trivy, kubeconform, plus the `actionlint` + `zizmor` workflow gate).

## Implementation history

- `#56` ECR `jenkins-percona` repo + safe untagged-only lifecycle (`terraform/ecr.tf`).
- `#57` GHA-OIDC image-push role (`terraform/iam-gha-jenkins-image-push.tf`) + CSI snapshot-controller addon.
- `#60` controller image build pipeline + OIDC push workflow (`images/jenkins/**`, `.github/workflows/build-jenkins-image.yml`).
- `#61` add `ecr:BatchGetImage` to the push role (pull-through for the pre-push local load).
- `#62` Stage 0 pilot — in-cluster chart consumes `jenkins:`-scoped values + render CI (`scripts/jenkins-chart-render-check.sh`).
- `#64` HYBRID reconciliation — force only the forks via `.jpi.override`, leave community soft.
