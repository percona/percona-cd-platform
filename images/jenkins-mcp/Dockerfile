# jenkins-mcp image: a forked mcp-jenkins server exposing Jenkins fleet tools
# over MCP and authenticating callers via Authentik OIDC. Reads for everyone,
# an operate (build-lifecycle) tier behind a flag for the jenkins-mcp-writers
# group, and no config/script mutation in any mode. Installed from the Percona
# fork at a pinned commit; run by the jenkins-mcp addon behind the jenkins-cd
# ALB. Deps baked in (no runtime install).
#
# Build (EKS nodes are amd64):
#   docker buildx build --platform linux/amd64 \
#     -t <account>.dkr.ecr.us-east-1.amazonaws.com/percona-cd/jenkins-mcp:0.2.7 \
#     --push images/jenkins-mcp
FROM ghcr.io/astral-sh/uv:python3.13-bookworm-slim

RUN uv pip install --system --no-cache \
    'mcp-jenkins @ git+https://github.com/nogueiraanderson/mcp-jenkins@10b43eb6247a2e020c3a193111ba0df9b7548e02'

USER 65532:65532
EXPOSE 9887
ENTRYPOINT ["mcp-jenkins"]
CMD ["--transport", "streamable-http", "--host", "0.0.0.0", "--port", "9887", "--read-only"]
