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
FROM ghcr.io/astral-sh/uv:python3.13-bookworm-slim

WORKDIR /app
COPY reconcile.py ./
RUN uv pip install --system --no-cache 'boto3==1.36.*' 'kubernetes==31.*'

USER 65532:65532
ENTRYPOINT ["python", "/app/reconcile.py"]
