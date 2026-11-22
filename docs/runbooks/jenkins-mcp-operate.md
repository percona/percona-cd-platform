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
  **any authenticated Percona user** (Authentik / JumpCloud SSO). There is no read group, and first login
  JIT-provisions the Authentik user. **Exception:** `get_item_config` (a job's raw config.xml) and
  `search_item_configs` (grep across many configs) are gated to **`jenkins-mcp-writers`**, because config.xml can carry plaintext secrets (e.g. the
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
   in operate mode if any returns 200 (fail-closed). See "Operate preflight states" below.
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
Authentik user via the JumpCloud SSO source). Authentik UI: Directory, Groups, `jenkins-mcp-writers`,
add the member. Their next minted token carries the `groups` claim and the operate tools unlock.
Revoke by removing them from the group.

Tell them to re-**authenticate**, not just reconnect. A reconnect reuses the cached token, which still
carries the old claims, so the operate tools keep returning "requires the jenkins-mcp-writers Authentik
group" until they log in again.

## Onboard a service account (automation reader)

For an automation that runs on a group-owned account with no JumpCloud identity (a scheduled
Claude routine, a CI job). The default login page is JumpCloud-only, so these accounts sign in
through a separate flow, `https://auth.cd.percona.com/if/flow/jenkins-mcp-service-login/`, that
accepts a username and an app password. The flow is gated by an expression policy to users of type
`service_account` in the `jenkins-mcp-service-accounts` group, so a human account or `akadmin` is
denied there, and a reputation policy denies repeated bad passwords from one IP or against one
username (score -5, expires 24h after the first failure). A locked-out account is restored early by
deleting its score: `GET $AK/policies/reputation/scores/?search=<name>`, then
`DELETE $AK/policies/reputation/scores/<pk>/`. Service accounts stay on the read tier: never add
one to `jenkins-mcp-writers`. The only exception is a per-job build grant (next section). They also reach only the masters named by their
`jenkins-mcp-master-<name>` groups. A service account with no such group reaches no master.

1. Declare the user in `resources/addons/authentik/templates/blueprint-jenkins-mcp.yaml`
   (`authentik_core.user`, `type: service_account`, group `jenkins-mcp-service-accounts`, one
   `jenkins-mcp-master-<name>` group per master it reads (declare the group if it is new), an `owner`
   attribute naming the person accountable for the key, and the two `token-*` attributes copied from
   `pmm-automation`), merge, and restart the Authentik worker so the blueprint applies
   ([authentik-blueprint-ops.md](authentik-blueprint-ops.md), gotcha 2).
2. Mint the app password once through the API (the blueprint never carries it). Put the owner and a
   rotation date in the token description:

   ```bash
   # The Secrets Manager copy carries an explicit deny for the admin role, read the cluster secret.
   TOK=$(kubectl --context percona-ci-platform -n authentik get secret authentik-config \
         -o jsonpath='{.data.AUTHENTIK_BOOTSTRAP_TOKEN}' | base64 -d)
   AK=https://auth.cd.percona.com/api/v3
   USER_PK=$(curl -s -H "Authorization: Bearer $TOK" "$AK/core/users/?username=<name>" | python3 -c 'import json,sys; print(json.load(sys.stdin)["results"][0]["pk"])')
   curl -s -H "Authorization: Bearer $TOK" -H "Content-Type: application/json" -X POST "$AK/core/tokens/" \
     -d "{\"identifier\":\"<name>-app-password\",\"intent\":\"app_password\",\"user\":$USER_PK,\"expiring\":false,\"description\":\"jenkins-mcp login, owner <person>, rotate by <YYYY-MM>\"}"
   curl -s -H "Authorization: Bearer $TOK" "$AK/core/tokens/<name>-app-password/view_key/"
   ```

   Hand the key to the named owner through the usual secret channel, never in Slack or Jira. The
   mint is attributed to `akadmin` in Authentik events, so note who ran it in the ticket.
3. The Jenkins connector must already exist in the Claude organization. On Team and Enterprise
   plans only an org Owner can add a custom connector (URL `https://jenkins-mcp.cd.percona.com/mcp`,
   client ID `jenkins-mcp`, blank secret). A member account, the shared one included, can only
   enable and connect it.
4. Tell the owner the connect order: open the flow URL in the browser that holds the shared Claude
   session, log in with the username and app password, then open the Jenkins connector and click
   Connect. The authorize step reuses that Authentik session. Opening the connector first lands on
   the JumpCloud-only page and fails.
5. Verify in Loki that the calls carry the service account's username, that an operate tool
   returns the `jenkins-mcp-writers` denial for it, and that a read on a master outside its scope
   returns `cannot reach master` (audit `status` `denied`).
6. Point the automation at `get_build_console_tail` or `export_build_log` for logs. An unlimited
   `get_build_console_output` returns the whole log in one response.

Revoke by setting `is_active: false` on the user in the blueprint (the login stage refuses inactive
users), then delete every token the account holds, not only the one minted above: a logged-in
account can create its own tokens, capped at 30 minutes by the `token-*` attributes.

```bash
curl -s -H "Authorization: Bearer $TOK" "$AK/core/tokens/?user__username=<name>" \
  | python3 -c 'import json,sys; [print(t["identifier"]) for t in json.load(sys.stdin)["results"]]' \
  | xargs -I{} curl -s -o /dev/null -w '{} %{http_code}\n' -H "Authorization: Bearer $TOK" -X DELETE "$AK/core/tokens/{}/"
```

## Grant a service account build on named jobs

A service account can start builds without the writers group, but only on the jobs listed for its
username under `serviceGrants` in `resources/addons/jenkins-mcp/values.yaml` (username, then master,
then job fullname). The grant covers `build_item` only. Stop, replay, cancel, the manage tier and
config reads stay writers-only, and the master scope still applies.

- `{}` grants the job with any string parameters.
- `require: [P]` refuses the call unless parameter `P` is present and non-blank.
- `allow: {P: [V]}` refuses a present `P` whose value is not in the list. Pair it with `require`
  for a guarded parameter, or omitting it lets Jenkins apply the job default.
- `pattern: {P: RE}` refuses a present `P` that does not fully match `RE`. Use it to force a single
  name in the form the create job generates (no comma lists).

A rule that names parameters refuses every other parameter of the job. List each parameter the
automation may send in `require`, `allow` or `pattern`, and leave out the ones it must never set.

Every parameter value must be a string, and a job with rules must be started with
`buildWithParameters`: the `/build` endpoint reads parameters from a submitted `json` form field
that the rules never see. A service account in `jenkins-mcp-writers` still gets only its grants.

Read the job's parameters and the names in its recent builds (`get_build_walk` with `parameters`)
before granting a job that deletes or stops anything. A merged values change rolls the pod through
ArgoCD, no image build needed. A malformed value makes the new pods fail at startup while the old
ones keep serving, and `helm template` does not check the rules, so load the rendered value first:

```bash
export MCP_JENKINS_SERVICE_GRANTS="$(helm template t resources/addons/jenkins-mcp --set aws.accountId=1 \
  | grep -A1 MCP_JENKINS_SERVICE_GRANTS | tail -1 | sed 's/^ *value: //' \
  | python3 -c 'import json,sys; print(json.loads(sys.stdin.read()))')"
(cd images/jenkins-mcp && uv run --frozen python -c 'from mcp_jenkins.core.grants import get_grants; print(len(get_grants()))')
```

A refused call returns the reason (audit `deny_reason` `not_granted`).

## Operate preflight states

In operate mode the gateway probes `/script` and `/view/all/newJob` on each master at start. Each
master gets one state. `list_masters` shows it in the `operate` field, and the start log has one
`operate preflight: <master> <state>` line per master.

| State | Cause | Effect |
|---|---|---|
| `verified` | Both probes answered 403 | All tools work for this master |
| `not verified` | A probe errored or answered something other than 200 or 403 (master down, 502, 401) | Read tools work. Operate and manage calls to this master are refused (audit `deny_reason` `operate_not_verified`) |
| `refused` | A probe answered 200 after start | Operate and manage calls to this master are refused until a restart |
| `not enabled` | The gateway runs read-only | No operate or manage tools are served |

A probe that answers 200 at start still refuses the whole start. A `not verified` master recovers
without a restart:

1. Wait until the master is up again.
2. Make an operate or manage call to it. The gateway probes it again, at most once per 60 s per master.
3. Call `list_masters`. Expect `operate: verified`.

Job Configure and Job Delete have no probe. Both are per-job permissions, and the job configure page
and `config.xml` answer 200 with Job/ExtendedRead alone, which the gateway holds. `/view/all/newJob`
(Job/Create) stays the proxy for job-definition rights.

## Authentication at start

The `sse` and `streamable-http` transports start only with all four of `MCP_OIDC_ISSUER`,
`MCP_OIDC_JWKS_URI`, `MCP_OIDC_AUDIENCE` and `MCP_PUBLIC_BASE_URL`. Without them the pod exits with
`Refusing to start ... without authentication: missing <names>`. Fix the chart values or the secret
and let the pod restart. A partly set configuration exits with `only partly set`.

`MCP_ALLOW_UNAUTHENTICATED_HTTP=1` allows an HTTP start with none of the four set, for local
development only. It never covers a partly set configuration. Never set it in a deployment.

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
  work: a loopback regex for Claude Code, two strict Cursor URIs, and the strict claude.ai callback
  for hosted Claude surfaces (web, Desktop, mobile). A new client with a different callback shape
  needs that list extended.
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

## Serve the metrics on their own port

The public ingress routes every path of the main port 9887, so `/metrics` on that port is public.
The chart value `metrics.port` moves `/metrics` to a port that the ingress does not route. The value
is empty by default, which keeps `/metrics` on 9887. ArgoCD applies a chart change at once, but an
image reaches the cluster later through its own bump pull request. Turn the port on in this order:

1. Merge the chart support with `metrics.port: ""`. The rendered manifests do not change.
2. Merge the bump pull request of the first image that reads `MCP_METRICS_PORT`. Wait until both
   pods run that image.
3. Set `metrics.port: 9888` in `resources/addons/jenkins-mcp/values.yaml` and merge.
4. Check that `https://jenkins-mcp.cd.percona.com/metrics` answers 404.
5. Check that Mimir still receives `mcp_tool_calls_total` from the namespace `jenkins-mcp`.

If step 3 lands before step 2, the ServiceMonitor scrapes a port that the old image does not serve,
and the metrics stop until the image arrives. To roll back, set `metrics.port: ""` again.

## Audit archive of write calls

Jenkins records the gateway's service account as the starter of every build the gateway starts.
The audit line in Loki names the person, and Loki keeps it for 30 days. The audit archive keeps the
record of every operate, manage and write call for 400 days. The gateway stores the same JSON as
the Loki line, one object per call, in the bucket `percona-ci-platform-jenkins-mcp-audit`
(`terraform/jenkins-mcp-audit.tf`). Reads are not stored. The key starts with the call time in UTC:
`audit/write/YYYY/MM/DD/YYYYMMDDTHHMMSS.ffffffZ-<request_id>.json`.

The gateway may only put objects under `audit/write/`. It cannot read, list or delete them. Each put
carries `If-None-Match` and the IAM policy requires that header, so S3 refuses to replace a stored record. A lifecycle rule deletes a record
after 400 days. The bucket has no versioning and no Object Lock.

The bucket name is the only switch. A record that does not reach S3 never fails or delays the tool
call. It increments `mcp_audit_archive_failures_total{reason="error|timeout|backlog"}` and logs a
warning that starts with `audit archive:` and names the S3 error code. Stored records increment
`mcp_audit_archive_stored_total`. The gateway reaches S3 on TCP 443 and gets its credentials from
the Pod Identity agent. The namespace NetworkPolicy allows both.

### Turn the archive on

Each change below is its own pull request. Merge one only when the check of the one before passed.

1. Merge the Terraform change. Run `just tf-plan`.
2. Check that the plan only adds the bucket `percona-ci-platform-jenkins-mcp-audit`, its five
   settings, the policy `percona-ci-platform-jenkins-mcp-audit`, and one attachment to the
   jenkins-mcp role.
3. Run `just tf-apply`.
4. Merge the bump pull request of the first image that reads `MCP_AUDIT_S3_BUCKET` (0.10.14 or
   later). Wait until both pods run that image.
5. Set `auditArchive.bucket: percona-ci-platform-jenkins-mcp-audit` in
   `resources/addons/jenkins-mcp/values.yaml` and merge.
6. Wait until both pods have the env `MCP_AUDIT_S3_BUCKET`:
   `kubectl -n jenkins-mcp get deploy jenkins-mcp -o jsonpath='{..env[?(@.name=="MCP_AUDIT_S3_BUCKET")].value}'`.
7. Run one write call, for example `cancel_queue_item` with a queue id that does not exist.
8. List the records of today:
   `aws s3 ls s3://percona-ci-platform-jenkins-mcp-audit/audit/write/$(date -u +%Y/%m/%d)/`.
   Expected result: one new object whose name ends in the `request_id` of the Loki audit line.
9. Check in Mimir that `mcp_audit_archive_stored_total` grew and
   `mcp_audit_archive_failures_total` did not.

If step 8 shows no object, read the pod log for `audit archive:`. `AccessDenied` points at the IAM
policy, `NoSuchBucket` at the chart value, and `timeout` at the NetworkPolicy or the S3 endpoint.

To turn the archive off, set `auditArchive.bucket: ""`. The stored records stay until they expire.

### Read one record back

Run the `aws` commands with read access to the bucket. The gateway's own role cannot read it.

1. Find the `request_id` of the call in Loki, or the day of the call in UTC.
2. Find the key: `aws s3 ls s3://percona-ci-platform-jenkins-mcp-audit/audit/write/YYYY/MM/DD/ | grep <request_id>`.
3. Print the record: `aws s3 cp s3://percona-ci-platform-jenkins-mcp-audit/<key> - | jq .`.

### Find who started, replayed or stopped a build

Use Loki for a build of the last 30 days. Use the archive for an older build. Run the `aws`
commands with read access to the bucket. The gateway's own role cannot read it.

1. Read the build's start time and cause from Jenkins:
   `<master>/job/<job>/<number>/api/json?tree=timestamp,actions[causes[userId,shortDescription]]`.
2. If the cause names the gateway's service account, convert `timestamp` (milliseconds) to a UTC
   date: `date -u -d @$(( <timestamp> / 1000 ))`.
3. Copy the records of that day and of the day before:

   ```sh
   aws s3 cp --recursive s3://percona-ci-platform-jenkins-mcp-audit/audit/write/2026/11/19/ ./audit/
   ```

4. Select the calls on the job:

   ```sh
   jq -c 'select(.args.fullname == "<job>" or any(.args.items[]?; .fullname == "<job>"))
     | [.ts, .tool, .status, .master, .preferred_username, .sub, .args.number, .args.items]' ./audit/*.json
   ```

5. For a started build, take the last `build_item` or `replay_build` call with status `ok` on that
   master before the build's `timestamp`. A build waits in the queue, so the call can be minutes
   older than the start.
6. For a stopped build, take the `stop_build` or `stop_builds` call that names the build number.
7. Read the person from `preferred_username` and `sub`.

If two calls on the same job fall in the queue window, the record cannot tell which one started
the build. Record both people and their times.

## Verify

```sh
# Operate preflight outcome (the per-master over-privilege gate):
kubectl -n jenkins-mcp logs deploy/jenkins-mcp --since=5m | grep -iE 'operate|preflight'
#   -> "Operate (write) mode enabled" (passed), or "Refusing operate ... over-privileged"
#      (a master grants too much; fix its matrix auth and restart).
#   -> one "operate preflight: <master> verified | not verified | refused" line per master.
```

Every call is audited to Loki under the caller's Authentik `sub` (event `mcp_tool_call`); an export
additionally emits an `mcp_export` line carrying the S3 object key, never the presigned URL.
