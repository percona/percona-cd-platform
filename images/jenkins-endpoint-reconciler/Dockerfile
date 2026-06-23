# jenkins-endpoint-reconciler image (ADR 0019, PS-10945). Reconciles EC2
# Jenkins master IPs into K8s EndpointSlices for the jenkins-ingress proxy.
# Pushed to ECR under percona-cd/, run by the reconciler CronJob
# (resources/addons/jenkins-endpoint-reconciler). Deps baked in (no runtime
# uv install), per the future-option note that previously lived in values.yaml.
#
# Build (EKS nodes are amd64):
#   docker buildx build --platform linux/amd64 \
#     -t <account>.dkr.ecr.us-east-1.amazonaws.com/percona-cd/jenkins-endpoint-reconciler:0.1.0 \
#     --push images/jenkins-endpoint-reconciler
# Base: uv + Python 3.13 (Debian bookworm slim). Pinned BY DIGEST so an upstream
# re-push of the same tag cannot change our base. amd64 platform digest (EKS
# nodes are amd64; build host may be arm64 -> buildx --platform linux/amd64).
# Refresh with: crane digest --platform linux/amd64 ghcr.io/astral-sh/uv:python3.13-bookworm-slim
FROM ghcr.io/astral-sh/uv:python3.13-bookworm-slim@sha256:847b5e690018bc6b9d97a0848da65f721b785f1e78d9c7067b8947c7010b2718

WORKDIR /app
COPY reconcile.py ./
RUN uv pip install --system --no-cache 'boto3==1.36.*' 'kubernetes==31.*'

USER 65532:65532
ENTRYPOINT ["python", "/app/reconcile.py"]
