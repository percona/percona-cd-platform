# jenkins-mcp: a first-party Percona MCP server exposing token-free Jenkins fleet
# tools over MCP, authenticating callers via Authentik OIDC. Reads for everyone,
# an operate (build-lifecycle) tier behind a flag for the jenkins-mcp-writers
# group, and no config/script mutation in any mode. Run by the jenkins-mcp addon
# behind the jenkins-cd ALB. Built from the vendored source in this directory
# (no runtime install, no external fork pull); deps locked via uv.lock.
#
# Build (EKS nodes are arm64 Graviton; CI builds this multi-arch on push to main):
#   docker buildx build --platform linux/amd64,linux/arm64 \
#     -t <account>.dkr.ecr.us-east-1.amazonaws.com/percona-cd/jenkins-mcp:<tag> \
#     --push images/jenkins-mcp
# Base: uv + Python 3.13 (Debian trixie slim). Pinned BY DIGEST so an upstream
# re-push of the same tag cannot change our base. Multi-arch INDEX digest (carries
# linux/amd64 + linux/arm64) so the one pin builds both platforms.
# Refresh with: docker buildx imagetools inspect ghcr.io/astral-sh/uv:python3.13-trixie-slim --format '{{.Manifest.Digest}}'
FROM ghcr.io/astral-sh/uv:python3.13-trixie-slim@sha256:803d2b80b6913e562837d69f037f39bfea80f51ef3f0dd0d8fdffbd95688fd6d

WORKDIR /app

# HOME must be writable by the runtime uid so the loguru file-sink setup works
# on a bare `docker run` (the addon also mounts a writable emptyDir here).
ENV HOME=/home/app
RUN mkdir -p /home/app && chown 65532:65532 /home/app

# Install deps first (this layer caches across source-only changes), then the
# project. LICENSE (AGPL-3.0) + NOTICE (retained upstream MIT attribution) are
# copied so the distributed image carries both.
COPY pyproject.toml uv.lock README.md LICENSE NOTICE ./
RUN uv sync --frozen --no-dev --no-install-project
COPY src ./src
RUN uv sync --frozen --no-dev

ENV PATH="/app/.venv/bin:$PATH"
EXPOSE 9887

# Non-root, matching the cd-platform first-party image convention.
USER 65532:65532

ENTRYPOINT ["mcp-jenkins"]
CMD ["--transport", "streamable-http", "--host", "0.0.0.0", "--port", "9887", "--read-only"]
