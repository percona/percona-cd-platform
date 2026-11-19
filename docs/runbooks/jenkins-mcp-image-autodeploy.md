# jenkins-mcp image auto-deploy

How a new jenkins-mcp container image reaches the cluster, and how to operate the pipeline.

## Flow

```
push to main (images/jenkins-mcp/**)       build-jenkins-mcp-image            human                  ArgoCD (jenkins-mcp App)
publish: build + smoke + push          ->  bump: prepare a verified      ->  open PR from the   ->  tracks HEAD, selfHeal:
0.x.y-<sha> to ECR                         branch with image.tag + digest    summary link, merge    syncs, pod rolls
```

1. A push to `main` that touches `images/jenkins-mcp/**` starts the `build-jenkins-mcp-image` workflow.
2. The `publish` job builds, smoke-boots and pushes `percona-cd/jenkins-mcp:<version>-<short-sha>` to ECR (immutable). It then smoke-tests the pushed digest.
3. The `bump` job prepares the branch `chore/deploy-jenkins-mcp-<run id>`. Its one commit sets `image.tag` and `image.digest` in `resources/addons/jenkins-mcp/values.yaml`.
4. The commit is created through the GitHub API (`createCommitOnBranch`). GitHub signs it, so it satisfies the signed-commits rule on `main`.
5. GitHub Actions may not create pull requests in this repository. The run ends green when the branch is ready, and its job summary holds a prefilled pull request link.
6. A person opens the pull request, reviews it and squash-merges it. This is the deploy gate.
7. The `jenkins-mcp` ArgoCD Application tracks `HEAD` with `selfHeal` and `prune`. After the merge it syncs, and the Deployment rolls the pods.

The Deployment pulls `repository:tag@digest` when `image.digest` is set. It falls back to the tag when the digest is empty.

## Why this shape

- `main` requires signed commits. A `createCommitOnBranch` commit is GitHub-verified, so CI needs no signing material.
- No controller and no extra credential. The workflow that builds the image also writes the deploy commit.
- Build is not deploy (ADR 0025). Merging is a human act. See [ADR 0038](../adr/0038-jenkins-mcp-image-autodeploy.md) for the decision and its amendment.
- A person opens the pull request, so `ci-gate` runs on it like on any other pull request.

## Operate

### Ship a code change

Bump `version` in `images/jenkins-mcp/pyproject.toml` and refresh `uv.lock` in the same commit as the
code. Two independent reasons:

- The gateway tests run `uv` with `--frozen`, so a lock that does not match `pyproject.toml` fails CI
  even though the same commands pass locally without the flag.
- The image tag is `<pyproject.version>-<short-sha>`. Skipping the version bump still produces a
  deployable tag, but one that cannot be traced back to a release.

### Deploy a new image

1. Open the `build-jenkins-mcp-image` run for your push to `main` in the Actions tab.
   Expected result: every job is green.
2. Open the summary of the `prepare deploy bump branch` job.
   Expected result: it names the branch `chore/deploy-jenkins-mcp-<run id>` and shows a diff.
3. Check the diff in the summary.
   Expected result: only the `image.tag` line and the `image.digest` line change.
4. Click "Open the pull request" in the summary.
   Expected result: GitHub shows a new pull request with the title and body filled in.
5. Create the pull request.
6. Wait for `ci-gate` on the pull request.
   Expected result: `ci-gate` passes.
7. Squash-merge the pull request.
8. Watch the `jenkins-mcp` Application in ArgoCD until it is synced and healthy.
   Expected result: `sync=Synced health=Healthy`.
9. Check that every pod runs the pinned digest (commands below).
   Expected result: each pod `imageID` ends with the `image.digest` value from `values.yaml`.

If the summary says "nothing to bump", `values.yaml` already pins this tag and digest. No branch was made.

### Confirm a deploy landed

```sh
grep -E '^  (tag|digest): ' resources/addons/jenkins-mcp/values.yaml
kubectl -n argocd get application jenkins-mcp -o jsonpath='sync={.status.sync.status} health={.status.health.status}{"\n"}'
kubectl -n jenkins-mcp get pods -l app.kubernetes.io/name=jenkins-mcp \
  -o jsonpath='{range .items[*]}{.metadata.name} {.status.containerStatuses[0].imageID}{"\n"}{end}'
curl -s -o /dev/null -w '%{http_code}\n' https://jenkins-mcp.cd.percona.com/healthz
```

The last command prints `200` when the gateway answers.

### Skip a deploy

Do not open the pull request. The image stays in ECR. Nothing deploys until a later bump is merged.

### Unused bump branches

Every push to `main` that rebuilds the image makes a new bump branch. When nobody opens its pull
request, the branch stays. Bump branches pile up over time.

1. List the bump branches.
   `git ls-remote --heads origin 'chore/deploy-jenkins-mcp-*'`
2. Keep the branch of the newest run that you still want to deploy.
3. Delete each other branch.
   `git push origin --delete chore/deploy-jenkins-mcp-<run id>`

You can also delete a branch on the GitHub "Branches" page of the repository.

### Re-create a bump branch

Re-run the `build-jenkins-mcp-image` workflow on `main` from the Actions tab ("Re-run all jobs"), or
push a no-op change under `images/jenkins-mcp/`. The `bump` job makes no branch if `values.yaml`
already pins the latest tag and digest.

### Roll back

Open a pull request that sets `image.tag` and `image.digest` back to the prior values (ECR keeps
prior images), or revert the merge commit of the bump pull request. ArgoCD syncs the reverted values
and the pods roll back.
