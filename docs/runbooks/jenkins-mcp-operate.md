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
  JIT-provisions the Authentik user.
- **Operate** (build, replay, stop, cancel) is served only to members of the **`jenkins-mcp-writers`**
  Authentik group, enforced per call.
- **Config and script mutation are never served**, in any mode.

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
   `JNKPercona` service user. On the target master ensure `JNKPercona` has `Overall/Read` plus
   folder-scoped `Job/Read` (add `Job/Build` and `Job/Cancel` only if that master should support the
   operate tier). It must NOT hold `Overall/RunScripts`, `Job/Configure`, `Job/Create`, or
   credentials permissions: the operate preflight probes `/script` and `/view/all/newJob` on every
   master and refuses to start the gateway in operate mode if any returns 200 (fail-closed).
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

## Verify

```sh
# Operate preflight outcome (the per-master over-privilege gate):
kubectl -n jenkins-mcp logs deploy/jenkins-mcp --since=5m | grep -iE 'operate|preflight'
#   -> "Operate (write) mode enabled" (passed), or "Refusing operate ... over-privileged"
#      (a master grants too much; fix its matrix auth and restart).
```

Every call is audited to Loki under the caller's Authentik `sub` (event `mcp_tool_call`); an export
additionally emits an `mcp_export` line carrying the S3 object key, never the presigned URL.
