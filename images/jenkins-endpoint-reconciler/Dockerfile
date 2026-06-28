# jenkins-endpoint-reconciler image (ADR 0019, PS-10945). Reconciles EC2
# Jenkins master IPs into K8s EndpointSlices for the jenkins-ingress proxy.
# Pushed to ECR under percona-cd/, run by the reconciler CronJob
# (resources/addons/jenkins-endpoint-reconciler). Deps baked in (no runtime
# uv install), per the future-option note that previously lived in values.yaml.
#
# Build (EKS nodes are arm64 Graviton; CI builds this multi-arch on push to main):
#   docker buildx build --platform linux/amd64,linux/arm64 \
#     -t <account>.dkr.ecr.us-east-1.amazonaws.com/percona-cd/jenkins-endpoint-reconciler:0.1.0 \
#     --push images/jenkins-endpoint-reconciler
# Base: uv + Python 3.13 (Debian bookworm slim). Pinned BY DIGEST so an upstream
# re-push of the same tag cannot change our base. Multi-arch INDEX digest (carries
# linux/amd64 + linux/arm64) so the one pin builds both platforms.
# Refresh with: docker buildx imagetools inspect ghcr.io/astral-sh/uv:python3.13-bookworm-slim --format '{{.Manifest.Digest}}'
FROM ghcr.io/astral-sh/uv:python3.13-bookworm-slim@sha256:531f855bda2c73cd6ef67d56b733b357cea384185b3022bd09f05e002cd144ca

WORKDIR /app
COPY reconcile.py ./
RUN uv pip install --system --no-cache 'boto3==1.36.*' 'kubernetes==31.*'

USER 65532:65532
ENTRYPOINT ["python", "/app/reconcile.py"]
