# Operating the jenkins-mcp gateway (fleet + access)

End state: an operator can add or remove a Jenkins master from the token-free MCP gateway, and
grant or revoke the build (operate) tier, with no code or image change. The gateway
(`resources/addons/jenkins-mcp/`) is one pod holding a single read-only Jenkins service
credential, so it can reach a master only if that master is in the fleet secret. Client setup and
the access model are in `images/jenkins-mcp/README.md`; the S3 export path in
[`jenkins-mcp-exports.md`](jenkins-mcp-exports.md); image rollout in
[`jenkins-mcp-image-autodeploy.md`](jenkins-mcp-image-autodeploy.md).

## Access model (recap)

- **Reads** (jobs, builds, logs, nodes, queue, views, and the S3 log/artifact export) are open to
  **any authenticated Percona user** (Authentik / Duo SSO). There is no read group, and first login
  JIT-provisions the Authentik user. **Exception:** `get_item_config` (a job's raw config.xml) is
  gated to **`jenkins-mcp-writers`**, because config.xml can carry plaintext secrets (e.g. the
  `<authToken>` remote-build-trigger token).
- **Operate** (build, replay, stop, cancel) and **manage** (create / update / delete job definitions)
  are served only with `--enable-operate`, gated per call to **`jenkins-mcp-writers`**. The manage
  tier is served but currently inert: the service identity lacks backend Create/Configure/Delete, so
  those calls return 403 (job management stays API/CLI-only for now, PS-11341).
- **Node-config and Groovy script-console mutation are never served**, in any mode.

## The fleet (which masters are reachable)

The reachable set is the AWS Secrets Manager secret `percona-ci-platform/jenkins-mcp/fleet`, not
code. External Secrets Operator syncs it to the k8s secret `jenkins-mcp-fleet`, mounted at
`/etc/jenkins-mcp/fleet.json`. Shape (the token is the only secret, never logged or committed):

```json
{"masters": [
  {"name": "ps80", "url": "https://ps80.cd.percona.com/", "username": "JNKPercona", "token": "<read api token>"}
]}
```

`list_masters` (an MCP tool) reports the configured set and live reachability.

## Add a master

1. **Grant the service account read on that master.** The gateway authenticates as the
   `JNKPercona` service user. On the target master ensure `JNKPercona` has `Overall/Read`,
   folder-scoped `Job/Read`, and `Job/ExtendedRead` (the last is required for `get_item_config` to
   read config.xml; add `Job/Build` and `Job/Cancel` only if that master should support the operate
   tier). For the Terraform-managed masters this grant lives in
   `resources/jenkins-masters/<inst>/init.groovy.d/matrix.groovy`. It must NOT hold
   `Overall/RunScripts`, `Job/Configure`, `Job/Create`, or credentials permissions: the operate
   preflight probes `/script` and `/view/all/newJob` on every master and refuses to start the gateway
   in operate mode if any returns 200 (fail-closed).
2. **Mint an API token for `JNKPercona` on that master** (Jenkins UI, the `JNKPercona` user,
   Security, Add new API token). Copy it once.
3. **Add the entry to the fleet secret.** Edit the JSON in the AWS console (Secrets Manager,
   `percona-ci-platform/jenkins-mcp/fleet`, Retrieve secret value then Edit) and add the master
   object. Do not write the token to disk or paste it elsewhere.
4. **Pick it up.** The fleet is cached at process start, so after ESO resyncs (about a minute)
   restart the pod:
   ```sh
   kubectl -n jenkins-mcp rollout restart deploy/jenkins-mcp
   ```
5. **Verify.** Call `list_masters` (the new master is listed and reachable), or run any read tool
   against it.

Removing a master is the reverse: drop its object from the secret, save, restart the pod.

## Onboard a writer (operate tier)

Reads need no action. To grant the build tier, add the user to the `jenkins-mcp-writers` Authentik
group **after they have logged into the gateway at least once** (first login JIT-provisions the
Authentik user via the Duo SSO source). Authentik UI: Directory, Groups, `jenkins-mcp-writers`,
add the member. Their next minted token carries the `groups` claim and the operate tools unlock.
Revoke by removing them from the group.

Tell them to re-**authenticate**, not just reconnect. A reconnect reuses the cached token, which still
carries the old claims, so the operate tools keep returning "requires the jenkins-mcp-writers Authentik
group" until they log in again.

## Do not grant Job/Create to activate the manage tier

The manage tools (`create_item`, `set_item_config`, `delete_item`) are served and writers-gated, but
the `JNKPercona` fleet account has no backend Create/Configure/Delete, so every manage call returns
403. Manage job definitions through the Jenkins API or CLI until this is activated.

Granting those permissions to `JNKPercona` today **crash-loops the gateway on its next restart**. The
operate preflight refuses to start when `/view/all/newJob` returns 200, so the grant that would make
the tools work is the same grant that trips the fail-closed gate. Activating the tier therefore needs
a `write_preflight()` rework alongside the backend grant, never the grant alone. Tracked in PS-11341.

## What is not in git

Most of the gateway is GitOps-managed, but three dependencies live outside this repo. Recreating the
service from source alone will not work without them.

- **The Authentik objects** (OAuth2 provider `jenkins-mcp-provider`, application `jenkins-mcp`, group
  `jenkins-mcp-writers`) were created through the bootstrap-token admin API and are adopted
  idempotently by the committed blueprint
  `resources/addons/authentik/templates/blueprint-jenkins-mcp.yaml`. The provider is a public (PKCE)
  client, so there is no client secret to rotate. Its redirect-URI allowlist is what makes a client
  work: a loopback regex for Claude Code plus two strict Cursor URIs. A new client with a different
  callback shape needs that list extended.
- **The fleet secret** `percona-ci-platform/jenkins-mcp/fleet` is hand-edited in AWS Secrets Manager
  (see The fleet, above). No Terraform resource owns its contents.
- **The ECR repository** `percona-cd/jenkins-mcp` was created by hand, while `terraform/ecr.tf`
  declares it with `image_tag_mutability = "IMMUTABLE"`. Until Terraform owns it, an apply that
  reaches that resource errors with AlreadyExists. Check, then import if it is still unmanaged:
  ```sh
  aws ecr describe-repositories --repository-names percona-cd/jenkins-mcp \
    --query 'repositories[0].imageTagMutability' --output text   # MUTABLE means not yet imported
  tofu import aws_ecr_repository.jenkins_mcp percona-cd/jenkins-mcp
  ```
  The import flips mutability in place and keeps the existing images. This is a deliberate raw-`tofu`
  exception to the justfile-only rule, since there is no `just tf-import` recipe. Run it from
  `terraform/` with `AWS_PROFILE` exported, and `just tf-state-backup` first.

## Verify

```sh
# Operate preflight outcome (the per-master over-privilege gate):
kubectl -n jenkins-mcp logs deploy/jenkins-mcp --since=5m | grep -iE 'operate|preflight'
#   -> "Operate (write) mode enabled" (passed), or "Refusing operate ... over-privileged"
#      (a master grants too much; fix its matrix auth and restart).
```

Every call is audited to Loki under the caller's Authentik `sub` (event `mcp_tool_call`); an export
additionally emits an `mcp_export` line carrying the S3 object key, never the presigned URL.
