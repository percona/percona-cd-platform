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

## Run

```
mcp-jenkins --transport streamable-http --host 0.0.0.0 --port 9887 --read-only
```

OIDC auth turns on when `MCP_OIDC_ISSUER` / `MCP_OIDC_JWKS_URI` / `MCP_OIDC_AUDIENCE` /
`MCP_PUBLIC_BASE_URL` are set; the fleet (masters + read-only tokens) is one JSON blob at
`MCP_JENKINS_FLEET_FILE`. In production this runs as the `jenkins-mcp` addon on the
percona-ci-platform EKS cluster, behind the `jenkins-cd` ALB.

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
