# jenkins-mcp

A token-free MCP gateway to Percona's Jenkins fleet. Developers connect from an MCP client
(Claude Code, Cursor, the Claude / ChatGPT desktop apps), log in once through Authentik
(Duo-backed OIDC), and read the fleet. The server holds a single read-only Jenkins credential
and injects it per call, so no user ever handles a Jenkins token.

## Access model

- Reads are open to any authenticated Percona user (Authentik / Duo SSO).
- The operate tier (build, replay, stop, cancel) is served only when the server runs with
  `--enable-operate`, and is gated per call to the `jenkins-mcp-writers` Authentik group.
- Config and script mutation are never exposed, in any mode.

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
  artifact to a short-lived signed S3 URL). The operate tier (build, replay, stop, cancel) is served
  only to members of the `jenkins-mcp-writers` Authentik group, enforced per call. Config and script
  mutation are never exposed. 33 tools total (29 read, 4 operate).
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

```
uv sync --all-extras --dev
uv run pytest
uv run ruff check . && uv run ruff format --check .
```

## Image

Built by CI (`.github/workflows/build-jenkins-mcp-image.yml`) from this directory and pushed to
ECR `percona-cd/jenkins-mcp` via GitHub Actions assuming an AWS IAM role over OIDC. The deployed
tag is bumped in `resources/addons/jenkins-mcp/values.yaml`.

## License

AGPL-3.0 (see `LICENSE`), matching the percona-cd-platform repository. This project began as a
derivative of [mcp-jenkins](https://github.com/lanbaoshen/mcp-jenkins) (MIT) and has been
substantially rewritten by Percona. The upstream MIT copyright is retained in `NOTICE`.
