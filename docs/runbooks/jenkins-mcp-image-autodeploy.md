# jenkins-mcp image auto-deploy

How a new jenkins-mcp container image reaches the cluster, and how to operate the pipeline.

## Flow

```
push to main (images/jenkins-mcp/**)       build-jenkins-mcp-image          human          ArgoCD (jenkins-mcp App)
publish: build + smoke + push          ->  bump: open a verified PR    ->  review +   ->  tracks HEAD, selfHeal:
0.x.y-<sha> to ECR                         bumping values.yaml image.tag    squash-merge   syncs, pod rolls
```

1. On a push to `main` touching `images/jenkins-mcp/**`, the `build-jenkins-mcp-image` workflow's `publish` job builds, smoke-boots, and pushes `percona-cd/jenkins-mcp:<version>-<short-sha>` to ECR (immutable), then smoke-tests the pushed digest.
2. The `bump` job (same workflow, `needs: publish`) opens a PR bumping `image.tag` in `resources/addons/jenkins-mcp/values.yaml` to the just-pushed tag. The commit is created via the GitHub API (`createCommitOnBranch`), so GitHub signs it and it satisfies main's required-signed-commits rule. No GitHub App, no GPG key, no PAT required.
3. A human reviews the one-line diff and squash-merges. This is the deploy gate. Nothing reaches the cluster un-reviewed.
4. The `jenkins-mcp` ArgoCD Application tracks `HEAD` with `selfHeal` + `prune`. On merge it syncs the new tag and the Deployment rolls the pod.

## Why this shape

- `main` requires signed commits. A `createCommitOnBranch` API commit is GitHub-verified, so it passes with no signing material in CI (the same mechanism `refresh-fork-locks.yml` uses).
- No standing controller, no extra credential, no Pod Identity. The deploy is authored by the same workflow that already builds the image.
- Build is not deploy (ADR 0025): merging is a human act, nothing auto-rolls. See [ADR 0038](../adr/0038-jenkins-mcp-image-autodeploy.md) for the decision and the rejected alternatives (notably ArgoCD Image Updater, which needs a GitHub App).

## Operate

### Ship a code change

Bump `version` in `images/jenkins-mcp/pyproject.toml` and refresh `uv.lock` in the same commit as the
code. Two independent reasons:

- The `test` job runs `uv` with `--frozen`, so a lock that does not match `pyproject.toml` fails CI
  even though the same commands pass locally without the flag.
- The image tag is `<pyproject.version>-<short-sha>`. Skipping the version bump still produces a
  deployable tag, but one that cannot be traced back to a release.

### Trigger or re-trigger a deploy PR

A deploy PR opens automatically on every push to `main` that rebuilds the image. To force one (for example after closing a stale PR), re-run the `build-jenkins-mcp-image` workflow on `main` from the Actions tab ("Re-run all jobs"), or push a no-op change under `images/jenkins-mcp/`. The `bump` job no-ops if `values.yaml` already pins the latest tag.

### Make ci-gate run on the bump PR (optional)

A PR opened with the default `GITHUB_TOKEN` does not re-trigger `ci.yml`, so `ci-gate` does not post on it. The image is already built, smoke-booted, and pushed by `publish` before the PR opens, so squash-merging the pre-validated one-line diff is safe. To have `ci-gate` run automatically, set the optional repo secret `JENKINS_MCP_BUMP_TOKEN` to a fine-grained PAT with contents + pull-requests write on this repo. The workflow uses it in preference to `GITHUB_TOKEN`. A personal PAT needs no GitHub App and no org owner.

### Skip a deploy

Close the bump PR. The image stays in ECR. Nothing deploys until a later bump PR is merged.

### Roll back

Open a PR setting `image.tag` back to the prior tag (ECR keeps prior tags), or revert the merge commit of the bump PR. ArgoCD syncs the reverted tag and the pod rolls back.

### Confirm a deploy landed

```sh
kubectl -n jenkins-mcp get deploy jenkins-mcp -o jsonpath='{.spec.template.spec.containers[0].image}{"\n"}'
kubectl -n argocd get application jenkins-mcp -o jsonpath='sync={.status.sync.status} health={.status.health.status}{"\n"}'
curl -s -o /dev/null -w '%{http_code}\n' https://jenkins-mcp.cd.percona.com/healthz
```
