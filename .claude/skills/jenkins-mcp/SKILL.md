---
name: jenkins-mcp
description: Operator context for the token-free Jenkins MCP gateway (vendored at images/jenkins-mcp/, addon at resources/addons/jenkins-mcp/, ArgoCD-deployed in namespace jenkins-mcp). Use when changing gateway code, shipping or rolling back its image, adding a Jenkins master to its fleet, granting the operate tier, or debugging a client that cannot connect. Triggers - jenkins-mcp, mcp gateway, jenkins-mcp-writers, operate tier, fleet secret, write_preflight, invalid_token from the gateway.
---

# jenkins-mcp (gateway operator context)

A token-free MCP gateway to the Jenkins fleet. Users authenticate through Authentik (Duo-backed
OIDC) and the server injects a single read-only Jenkins credential per call, so no user handles a
Jenkins token.

The component spans four places in this repo: the vendored Python source at `images/jenkins-mcp/`,
the Helm addon at `resources/addons/jenkins-mcp/`, the ECR repository in `terraform/ecr.tf`, and the
Authentik blueprint at `resources/addons/authentik/templates/blueprint-jenkins-mcp.yaml`.

## Read these, do not re-derive them

Facts (versions, tool counts, tier membership, access model) live in the docs below. Read the
relevant one before answering from memory, since they change per release.

| Task | Document |
| --- | --- |
| Connect a client, access model, troubleshoot a connection | `images/jenkins-mcp/README.md` |
| Add or remove a master, onboard a writer, what is not in git | `docs/runbooks/jenkins-mcp-operate.md` |
| Ship, verify, or roll back an image | `docs/runbooks/jenkins-mcp-image-autodeploy.md` |
| Log and artifact export to presigned S3 | `docs/runbooks/jenkins-mcp-exports.md` |
| Why the pipeline, export, and in-artifact tools are shaped this way | ADRs 0038, 0039, 0040 |

## Invariants worth stating before you touch anything

- **Version and lock move together.** A code change bumps `pyproject.toml` `version` and refreshes
  `uv.lock` in the same commit. CI runs `uv` with `--frozen`, so a stale lock passes locally and
  fails in CI.
- **The preflight is fail-closed, and it fights the manage tier.** `write_preflight()` refuses to
  start the gateway in operate mode if any master's `/script` or `/view/all/newJob` returns 200.
  Granting the fleet account Job/Create to make the manage tools work therefore crash-loops the
  gateway on its next restart. The grant and a preflight rework land together or not at all
  (PS-11341).
- **Build is not deploy.** Merging to `main` publishes an image and opens a one-line bump PR. Nothing
  reaches the cluster until a human merges that PR (ADR 0025, ADR 0038).
- **Three dependencies are not in git**: the Authentik objects, the AWS Secrets Manager fleet secret,
  and the manually created ECR repository. See "What is not in git" in the operate runbook before
  assuming the repo describes the whole service.
- **Group changes need a fresh login.** Adding someone to `jenkins-mcp-writers` takes effect on their
  next authentication, not on a reconnect with a cached token.
- **Editing anything under `images/jenkins-mcp/` triggers an image build**, including the README,
  which the Dockerfile copies into the image. Expect a deploy PR from a docs-only change there.
