# MTR test-history ingest image (PS-10541). Run by the mtr-ingest CronJob
# (resources/addons/mtr). Pushed to ECR under the percona-cd/ namespace:
#   <account>.dkr.ecr.<region>.amazonaws.com/percona-cd/mtr-ingest:<tag>
#
# Build (EKS nodes are amd64; build host may be arm64):
#   aws ecr get-login-password --region us-east-1 | docker login --username AWS --password-stdin <account>.dkr.ecr.us-east-1.amazonaws.com
#   docker buildx build --platform linux/amd64 \
#     -t <account>.dkr.ecr.us-east-1.amazonaws.com/percona-cd/mtr-ingest:0.1.0 \
#     --push images/mtr-ingest
# Base: uv + Python 3.13 (Debian bookworm slim). Pinned BY DIGEST so an upstream
# re-push of the same tag cannot change our base. Multi-arch INDEX digest (carries
# linux/amd64 + linux/arm64) so the one pin builds both platforms.
# Refresh with: docker buildx imagetools inspect ghcr.io/astral-sh/uv:python3.13-bookworm-slim --format '{{.Manifest.Digest}}'
FROM ghcr.io/astral-sh/uv:python3.13-bookworm-slim@sha256:531f855bda2c73cd6ef67d56b733b357cea384185b3022bd09f05e002cd144ca

WORKDIR /app
COPY pyproject.toml ./
COPY mtr_history ./mtr_history

# Install the package + the Postgres extra into the system environment; the
# `mtr-backfill` console script lands on PATH. uv is the project standard.
RUN uv pip install --system --no-cache '.[pg]'

# Run unprivileged; matches the CronJob securityContext (runAsUser 65532).
USER 65532:65532
ENTRYPOINT ["mtr-backfill"]
