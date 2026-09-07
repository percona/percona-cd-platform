# jenkins-mcp

A token-free MCP gateway to Percona's Jenkins fleet. Developers connect from an MCP client
(Claude Code, Cursor, the Claude / ChatGPT desktop apps), log in once through Authentik
(Duo-backed OIDC), and read the fleet. The server holds a single read-only Jenkins credential
and injects it per call, so no user ever handles a Jenkins token.

## Access model

- Reads are open to any authenticated Percona user (Authentik / Duo SSO), with one exception:
  `get_item_config` (a job's raw config.xml) is gated to `jenkins-mcp-writers`, because config.xml can
  carry plaintext secrets (e.g. the `<authToken>` remote-build-trigger token).
- The operate tier (build, replay, stop, cancel) and the manage tier (create, update, delete job
  definitions) are served only when the server runs with `--enable-operate`, and are gated per call
  to the `jenkins-mcp-writers` Authentik group.
- Job management is API-only for now. The manage tools are served but the gateway identity lacks
  backend Create/Configure/Delete, so they return 403. Manage job definitions through the Jenkins
  API/CLI directly until this is activated (PS-11341).
- Node-config and script-console mutation are never exposed, in any mode. The manage tier still lets a
  writer define a job that runs code on a master, so `jenkins-mcp-writers` is a code-execution-capable
  group and should be curated accordingly.

## Connect your MCP client

The gateway speaks MCP over streamable HTTP at `https://jenkins-mcp.cd.percona.com/mcp`. It is
token-free: you authenticate once in your browser through Authentik (Duo-backed OIDC), and the
server injects its own read-only Jenkins credential per call. You never create, paste, or store a
Jenkins token.

Facts that apply to every client:

- **One-time browser login.** On first connect the client opens an Authentik page, you complete the
  Duo prompt, and the session is cached (it refreshes silently afterwards).
- **Pre-registered OAuth client, no Dynamic Client Registration.** Authentik does not support DCR,
  so the gateway uses one pre-registered public (PKCE) client, `client_id: jenkins-mcp`, with no
  client secret. Every client must be told this id explicitly (nothing auto-discovers it), and the
  field name differs per client: Claude Code uses `--client-id` / `oauth.clientId`, Cursor uses
  `auth.CLIENT_ID`. Omit it and the client falls back to DCR and fails with "does not support
  dynamic client registration".
- **Access.** Reads are open to any authenticated Percona user (including downloading a large log or
  artifact to a short-lived signed S3 URL), except `get_item_config` (raw config.xml), which is gated
  to `jenkins-mcp-writers`. The operate tier (build, replay, stop, cancel) and the manage tier (create,
  update, delete job definitions) are served only to members of the `jenkins-mcp-writers` Authentik
  group, enforced per call. Node-config and script mutation are never exposed. 41 tools total (34 read,
  4 operate, 3 manage).
- **Master selection.** Pick a master per call with the `master` argument (for example
  `master: "ps80"`), or pin a session default by sending the `x-jenkins-master` header. Allowlisted
  names only. Call `list_masters` to see them.

### Claude Code

Add the gateway as a remote HTTP server, supplying the pre-registered public client id (Authentik
has no DCR, so this is required, and there is no client secret):

```sh
claude mcp add --transport http --client-id jenkins-mcp \
  jenkins-mcp https://jenkins-mcp.cd.percona.com/mcp
```

Then authenticate: inside Claude Code run `/mcp`, select `jenkins-mcp`, and complete the browser
Duo login. From Claude Code v2.1.186 you can instead log in from the shell (add `--no-browser` over
SSH, then paste the callback URL back):

```sh
claude mcp login jenkins-mcp
```

If your Claude Code build does not discover Authentik's per-application OIDC metadata
automatically, pin it explicitly with `add-json` (the `authServerMetadataUrl` override needs
v2.1.64+):

```sh
claude mcp add-json jenkins-mcp '{
  "type": "http",
  "url": "https://jenkins-mcp.cd.percona.com/mcp",
  "oauth": {
    "clientId": "jenkins-mcp",
    "authServerMetadataUrl": "https://auth.cd.percona.com/application/o/jenkins-mcp/.well-known/openid-configuration"
  }
}'
```

The OAuth callback uses a loopback port that Authentik already allowlists
(`http://localhost:<any-port>/callback`), so no redirect setup is needed. Add `--scope user` to the
`add` command to make the server available across all your projects, or add
`"headers": {"x-jenkins-master": "ps80"}` to the JSON form to send a default master.

### Cursor

Add a remote streamable-HTTP server to `~/.cursor/mcp.json` (all projects) or `.cursor/mcp.json`
(one project):

```json
{
  "mcpServers": {
    "jenkins-mcp": {
      "url": "https://jenkins-mcp.cd.percona.com/mcp",
      "auth": { "CLIENT_ID": "jenkins-mcp" },
      "headers": { "x-jenkins-master": "ps80" }
    }
  }
}
```

The `auth.CLIENT_ID` field (capitalised, and distinct from Claude Code's `oauth.clientId`) pins the
pre-registered public client so Cursor skips DCR. Without it Cursor falls back to Dynamic Client
Registration and fails with "does not support dynamic client registration". After editing, fully
restart Cursor (not just toggle the server), then click Connect for the browser Duo login. The
gateway already allowlists Cursor's redirect URIs, so no redirect setup is needed. The `headers`
block is optional. Drop it to choose a master per call instead. If you hit `invalid_scope`, update
Cursor to v2.6.19 or later.

### Claude (web, Desktop, mobile)

On the hosted Claude surfaces, add a custom connector by URL:
`https://jenkins-mcp.cd.percona.com/mcp`. Under the connector's Advanced settings, set the OAuth
client ID to `jenkins-mcp` and leave the client secret blank (Authentik has no DCR, so the id is
required; the gateway is a public client with no secret). Save, then click Connect for the browser
Duo login. The gateway already allowlists the hosted callback
(`https://claude.ai/api/mcp/auth_callback`), so no redirect setup is needed. On Team or Enterprise
plans only an org Owner can add a custom connector; members then enable it individually.

### pi.dev

Pi does not ship MCP support in its core, so this path uses the community
[`pi-mcp-adapter`](https://github.com/nicobailon/pi-mcp-adapter) extension and is less
battle-tested than the two above (treat it as unverified for this gateway). Install the adapter,
then add the gateway to `~/.config/mcp/mcp.json`:

```json
{
  "mcpServers": {
    "jenkins-mcp": {
      "url": "https://jenkins-mcp.cd.percona.com/mcp",
      "auth": "oauth",
      "oauth": {
        "grantType": "authorization_code",
        "clientId": "jenkins-mcp",
        "scope": "openid profile offline_access",
        "redirectUri": "http://localhost:3118/callback"
      }
    }
  }
}
```

The `redirectUri` port is free to choose (Authentik allowlists any
`http://localhost:<port>/callback`). The first connection opens the Authentik / Duo login. Select a
master with the per-call `master` argument.

### Troubleshooting a connection

**Opening the endpoint URL in a browser always returns `invalid_token`.** That is not a fault. The
endpoint speaks MCP, not HTML, and it has no browser-facing page. The login happens inside your MCP
client, never by visiting the URL. Ignore what the browser shows and connect from the client.

**`invalid_token` from the client itself** ("clear authentication tokens in your MCP client and
reconnect") means a stale registration or an expired refresh token. In Claude Code, clear it and
start over:

```sh
claude mcp list                 # the name you registered it under may differ
claude mcp logout jenkins-mcp   # drop the cached OAuth credentials
claude mcp remove jenkins-mcp   # add -s local|user|project if it was scoped
```

Then re-add it as above and authenticate again. `claude mcp get jenkins-mcp` health-checks the result.
In Cursor, delete the entry from `mcp.json`, fully restart Cursor (toggling the server is not enough),
then click Connect.

**"does not support dynamic client registration"** means the client id is missing. Authentik has no
DCR, so every client must be told `jenkins-mcp` explicitly, and the field name differs per client
(`--client-id` / `oauth.clientId` in Claude Code, `auth.CLIENT_ID` in Cursor).

**A tool returns 403 or "requires the jenkins-mcp-writers Authentik group"** means the call needs the
operate, manage, or config-read tier. Ask in #opensource-jenkins to be added to
`jenkins-mcp-writers`. You need an Authentik account first, which is created automatically
on your first Duo SSO login, so connect and authenticate before asking. Once added, disconnect and
re-authenticate so the new token carries the group claim. Reconnecting alone does not refresh it.
One exception: the manage tools (`create_item`, `set_item_config`, `delete_item`) return 403 even
for writers, because the backend grant is pending (PS-11341). That 403 is not a membership problem.
Manage job definitions through the Jenkins API/CLI instead.

**A transient 503 right after a gateway release** is the ALB re-registering its target, which takes
roughly 60 to 90 seconds. Retry before reporting it.

### Auto-deploy

New gateway image versions roll out on their own: when a new `percona-cd/jenkins-mcp` image is
pushed to ECR, the build workflow opens a bump PR against the addon `values.yaml`, and merging it
lets ArgoCD sync the new tag. The endpoint URL is stable, so a server-side image bump needs no
client change. See
[`../../docs/runbooks/jenkins-mcp-image-autodeploy.md`](../../docs/runbooks/jenkins-mcp-image-autodeploy.md).

## Run

```
mcp-jenkins --transport streamable-http --host 0.0.0.0 --port 9887 --read-only
```

OIDC auth turns on when `MCP_OIDC_ISSUER` / `MCP_OIDC_JWKS_URI` / `MCP_OIDC_AUDIENCE` /
`MCP_PUBLIC_BASE_URL` are set; the fleet (masters + read-only tokens) is one JSON blob at
`MCP_JENKINS_FLEET_FILE`. In production this runs as the `jenkins-mcp` addon on the
percona-ci-platform EKS cluster, behind the `jenkins-cd` ALB.

To run it locally, point it at a one-master fleet file and leave the OIDC vars unset, so it runs
WITHOUT auth (local dev only):

```sh
echo '{"masters":[{"name":"ps80","url":"https://ps80.cd.percona.com/","username":"JNKPercona","token":"<api token>"}]}' > fleet.json
MCP_JENKINS_FLEET_FILE=fleet.json uv run mcp-jenkins \
  --transport streamable-http --host 127.0.0.1 --port 9887 --read-only
curl -s http://127.0.0.1:9887/healthz   # -> OK
```

Operators: to add or remove a master, or grant the build (operate) tier, see
[`../../docs/runbooks/jenkins-mcp-operate.md`](../../docs/runbooks/jenkins-mcp-operate.md).

## Develop

CI runs these with `--frozen`, so run them that way locally too. Without it a stale `uv.lock` passes
here and fails in CI:

```
uv sync --all-extras --dev --frozen
uv run --frozen pytest -q
uv run --frozen ruff check . && uv run --frozen ruff format --check .
```

Any change that should reach the cluster must bump `version` in `pyproject.toml` **and** refresh
`uv.lock` in the same commit. The image tag is `<pyproject.version>-<short-sha>`, so shipping without
the version bump produces a tag that is confusing to trace back.

## Image

Built by CI (`.github/workflows/build-jenkins-mcp-image.yml`) from this directory and pushed to
ECR `percona-cd/jenkins-mcp` via GitHub Actions assuming an AWS IAM role over OIDC. The deployed
tag is bumped in `resources/addons/jenkins-mcp/values.yaml`.

## License

AGPL-3.0 (see `LICENSE`), matching the percona-cd-platform repository. This project began as a
derivative of [mcp-jenkins](https://github.com/lanbaoshen/mcp-jenkins) (MIT) and has been
substantially rewritten by Percona. The upstream MIT copyright is retained in `NOTICE`.
