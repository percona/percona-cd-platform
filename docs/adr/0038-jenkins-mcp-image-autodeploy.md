<!-- Copyright (C) 2026 Percona LLC -->
# 0038: Auto-deploy the jenkins-mcp image via a CI-authored bump PR

**Status:** Accepted (2026-06-23)
**Related:** [ADR 0025](0025-singleton-controller-rollout-gating.md) (the "build is not deploy" gap this closes for a stateless addon, see the amendment there bounding this exception), [ADR 0005](0005-gitops-bridge-bootstrap.md) (the addons ApplicationSet whose `aws.accountId` broadcast and `selfHeal` posture jenkins-mcp stays inside), [ADR 0027](0027-baked-jenkins-controller-image.md) (sibling first-party ECR image, but a singleton controller, so explicitly NOT a candidate for this auto-deploy path).

> **Implementation status: NOT YET MERGED (2026-06-23).** The build pipeline (`.github/workflows/build-jenkins-mcp-image.yml`) already pushes `0.3.0-<short-sha>` on every push to `main`; the deploy half is the manual `image.tag` edit in `resources/addons/jenkins-mcp/values.yaml`. This ADR records the decision to automate the PR-authoring half with a `bump` job in that same workflow.

## Context

The jenkins-mcp gateway is built and pushed by `.github/workflows/build-jenkins-mcp-image.yml`: every push to `main` re-smokes and publishes `percona-cd/jenkins-mcp:0.3.0-<short-sha>` to its ECR repo (`terraform/ecr.tf`, `IMMUTABLE` + `scan_on_push`, untagged-only lifecycle so a deployed tag never expires). But ArgoCD reconciles git, not ECR, so a freshly built image sits in ECR undeployed until a human bumps `image.tag` in `resources/addons/jenkins-mcp/values.yaml` and opens a PR. That manual bump is the "build is not deploy" split of [ADR 0025](0025-singleton-controller-rollout-gating.md) showing up operationally. For a stateless `replicaCount: 1` addon whose roll drops only a few in-flight MCP requests, the manual edit buys no safety, it only adds latency and a forgettable step.

Three repo constraints shape the mechanism:

1. **`main` requires signed commits** (`required_signatures` is enabled). Any automated write-back must produce GitHub-verified commits.
2. **`main` requires the `ci-gate` check** (and is strict, branch-up-to-date). The merge stays gated.
3. **jenkins-mcp lives in the addons ApplicationSet** ([ADR 0005](0005-gitops-bridge-bootstrap.md), `selfHeal: true` + `prune: true`), which broadcasts `aws.accountId` into it. The deploy path must keep it there and must not be reverted by selfHeal.

## Decision

Add a `bump` job to `build-jenkins-mcp-image.yml`, mirroring the repo's existing `refresh-fork-locks.yml` auto-PR pattern. After `publish` builds, smoke-boots, and pushes the image, `bump` opens a PR setting `image.tag` to the just-pushed tag. A human squash-merges it; ArgoCD syncs the merged `HEAD` as it already does. Only the PR's authoring is automated.

1. **The bump runs in CI, not a controller.** It is a job in the same workflow that already produces the image, so it knows the exact pushed tag with no ECR polling and no read credential.
2. **The commit is GitHub-verified via `createCommitOnBranch`.** The GitHub GraphQL API signs commits it creates with GitHub's own key, so the bump PR satisfies the required-signed-commits rule with no GPG key and no GitHub App. This is the exact mechanism `refresh-fork-locks.yml` already uses against `main`.
3. **Default credential is the built-in `GITHUB_TOKEN`.** No new secret is required. An optional fine-grained PAT (`JENKINS_MCP_BUMP_TOKEN`, contents + pull-requests write on this repo) can be set so the bump PR re-triggers `ci-gate`; without it the commit is still verified and the PR still opens, and the image is already validated by `publish`.
4. **The merge stays human; ArgoCD sync stays as-is.** The bump opens the PR; a human squash-merges; the addons ApplicationSet syncs `HEAD` under its current `selfHeal` policy. jenkins-mcp is not carved out of the appset, so the `aws.accountId` broadcast is untouched.

## Alternatives considered and rejected

- **ArgoCD Image Updater with a GitHub App (git PR write-back).** A controller in the `argocd` namespace watching ECR, writing back via a GitHub App for verified commits. Rejected on two grounds: creating and installing a GitHub App is a GitHub org-owner action that the platform operator cannot self-serve (it would require an IT request), and it adds standing infrastructure (a controller, a Pod Identity role, an ESO-synced App private key) to do what a job in the existing build workflow does with `createCommitOnBranch` and no new credential.
- **Image Updater `argocd` write-back method (parameter override, no git).** Reverted by the addons appset `selfHeal` ([ADR 0005](0005-gitops-bridge-bootstrap.md)); the deploy would flap. Rejected.
- **Direct git push to `main`.** Blocked by branch protection and required signed commits, and it would deploy with no review. Rejected.
- **A dedicated unprotected deploy branch with jenkins-mcp carved out of the appset.** Loses the appset's `aws.accountId` broadcast and splits the repo into a reviewed `main` and an unreviewed deploy branch for one addon. Rejected.
- **Stay fully manual (status quo).** The per-version `image.tag` edit is precisely the step the ask removes. Rejected.

## Consequences

**(+) The deploy stays a reviewed, signed, merged git change.** A human squash-merges a one-line diff; the commit is GitHub-verified. With the optional PAT, `ci-gate` also runs on the bump PR. Only the PR's authoring moves from a human to CI, so the principle of [ADR 0025](0025-singleton-controller-rollout-gating.md) holds.

**(+) No new standing infrastructure and no new privileged credential.** No controller, no Pod Identity role, no App key in Secrets Manager. The default path uses the built-in `GITHUB_TOKEN`.

**(+) jenkins-mcp stays inside the addons ApplicationSet**, so the `aws.accountId` broadcast and the existing sync posture are untouched ([ADR 0005](0005-gitops-bridge-bootstrap.md)).

**(-) Without the optional PAT, `ci-gate` does not auto-run on the bump PR.** A `GITHUB_TOKEN`-opened PR does not re-trigger `ci.yml`. The image is already built, smoke-booted, and pushed by `publish` before the PR opens, so the bump is pre-validated; a reviewer squash-merges it. Setting `JENKINS_MCP_BUMP_TOKEN` restores automatic `ci-gate` on the PR.

**(-) A deploy PR opens on every push to `main` that rebuilds the image**, including a docs-only change under `images/jenkins-mcp/` (the tag is sha-based). Such a PR redeploys a functionally identical image or can be closed. The scope is the stateless `jenkins-mcp` addon only, never a singleton Jenkins master (whose rollout stays gated by [ADR 0025](0025-singleton-controller-rollout-gating.md)).

## Acceptance criteria (verify once merged)

- A push to `main` that publishes `0.3.0-<short-sha>` results in a bump PR within the same workflow run, with a GitHub-verified commit (the "Verified" badge), touching only `image.tag` in `resources/addons/jenkins-mcp/values.yaml`.
- Squash-merging the PR leaves the `jenkins-mcp` Application `Synced` on the new tag and the pod running the new image.
- No push lands directly on `main`; every bump arrives as a reviewable PR.
- jenkins-mcp still resolves a non-empty `aws.accountId` from the addons appset broadcast (it was not carved out).
- `just ci` passes.

## References

- Build + bump workflow: [`.github/workflows/build-jenkins-mcp-image.yml`](../../.github/workflows/build-jenkins-mcp-image.yml)
- Pattern this mirrors: [`.github/workflows/refresh-fork-locks.yml`](../../.github/workflows/refresh-fork-locks.yml)
- Deploy target: `resources/addons/jenkins-mcp/values.yaml` (`image.tag`)
- ECR repo + lifecycle: `terraform/ecr.tf` (`aws_ecr_repository.jenkins_mcp`)
- Operator procedure: [docs/runbooks/jenkins-mcp-image-autodeploy.md](../runbooks/jenkins-mcp-image-autodeploy.md)
