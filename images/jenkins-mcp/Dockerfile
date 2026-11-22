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
# Two stages. The build stage is uv on Debian trixie slim. It installs a uv-managed
# standalone CPython 3.13 under /python and the locked venv under /app. The runtime
# stage is distroless cc (glibc, libstdc++, no shell, no package manager) and gets
# only /python and /app, so the Debian packages of the build stage do not ship.
# Both FROM lines are pinned BY multi-arch INDEX digest (linux/amd64 + linux/arm64),
# and Dependabot (docker, /images/jenkins-mcp) bumps both. The CPython patch release
# is the newest 3.13 that the pinned uv knows, and uv checks its hash on download.
# Refresh a pin with: docker buildx imagetools inspect <image>:<tag> --format '{{.Manifest.Digest}}'
FROM ghcr.io/astral-sh/uv:python3.13-trixie-slim@sha256:803d2b80b6913e562837d69f037f39bfea80f51ef3f0dd0d8fdffbd95688fd6d AS build

ENV UV_PYTHON=3.13 \
    UV_PYTHON_PREFERENCE=only-managed \
    UV_PYTHON_INSTALL_DIR=/python \
    UV_LINK_MODE=copy
# The runtime needs no pip. uv installs the venv, and pip's vendored copies of
# other packages would ship as findings of their own.
RUN uv python install \
 && rm -rf /python/cpython-3.13.*/bin/pip* \
           /python/cpython-3.13.*/lib/python3.13/site-packages/pip \
           /python/cpython-3.13.*/lib/python3.13/site-packages/pip-*.dist-info

WORKDIR /app

# Install deps first (this layer caches across source-only changes), then the
# project. LICENSE (AGPL-3.0) + NOTICE (retained upstream MIT attribution) are
# copied so the distributed image carries both.
COPY pyproject.toml uv.lock README.md LICENSE NOTICE ./
RUN uv sync --frozen --no-dev --no-install-project
COPY src ./src
RUN uv sync --frozen --no-dev

# HOME must be writable by the runtime uid so the loguru file-sink setup works
# on a bare `docker run` (the addon also mounts a writable emptyDir here).
RUN mkdir -p /home/app

FROM gcr.io/distroless/cc-debian13:nonroot@sha256:cb87acc574abe719b6e5c547384defa2f7b1f24af9605d3f618cf852495d8be2

COPY --from=build /python /python
COPY --from=build /app /app
COPY --from=build --chown=65532:65532 /home/app /home/app

WORKDIR /app
ENV HOME=/home/app
ENV PATH="/app/.venv/bin:$PATH"
EXPOSE 9887

# Non-root, matching the cd-platform first-party image convention.
USER 65532:65532

ENTRYPOINT ["mcp-jenkins"]
CMD ["--transport", "streamable-http", "--host", "0.0.0.0", "--port", "9887", "--read-only"]
