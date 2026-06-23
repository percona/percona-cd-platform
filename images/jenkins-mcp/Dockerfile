# jenkins-mcp: a first-party Percona MCP server exposing token-free Jenkins fleet
# tools over MCP, authenticating callers via Authentik OIDC. Reads for everyone,
# an operate (build-lifecycle) tier behind a flag for the jenkins-mcp-writers
# group, and no config/script mutation in any mode. Run by the jenkins-mcp addon
# behind the jenkins-cd ALB. Built from the vendored source in this directory
# (no runtime install, no external fork pull); deps locked via uv.lock.
#
# Build (EKS nodes are amd64; CI does this on push to main):
#   docker buildx build --platform linux/amd64 \
#     -t <account>.dkr.ecr.us-east-1.amazonaws.com/percona-cd/jenkins-mcp:<tag> \
#     --push images/jenkins-mcp
FROM ghcr.io/astral-sh/uv:python3.13-bookworm-slim

WORKDIR /app

# Install deps + the package into /app/.venv from the lockfile (reproducible, frozen).
COPY pyproject.toml uv.lock README.md ./
COPY src ./src
RUN uv sync --frozen --no-dev

ENV PATH="/app/.venv/bin:$PATH"
EXPOSE 9887

# Non-root, matching the cd-platform first-party image convention.
USER 65532:65532

ENTRYPOINT ["mcp-jenkins"]
CMD ["--transport", "streamable-http", "--host", "0.0.0.0", "--port", "9887", "--read-only"]
