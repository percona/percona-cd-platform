# mtr-ingest

Container image for the MySQL MTR test-history ingest job (PS-10541). It packages
the Percona `mtr-history` Python application and runs its `mtr-backfill` console
script: it fetches per-build MTR results from Jenkins, parses the JUnit XML into a
normalized schema, and writes the history to Postgres (and/or emits OpenMetrics)
so the Grafana failure matrix can render test history over time.

## What it is built from

- **Base image:** `ghcr.io/astral-sh/uv:python3.13-bookworm-slim`, pinned by
  multi-arch index digest so an upstream re-push of the same tag cannot change
  our base. The base bundles [uv](https://github.com/astral-sh/uv)
  (Apache-2.0 OR MIT), CPython 3.13 (PSF-2.0), and the Debian "bookworm" slim
  userland.
- **Our code:** `mtr_history/`, the original Percona application (AGPL-3.0).
- **Python dependencies** (installed at build with `uv pip install --system '.[pg]'`):
  `click` (BSD-3-Clause), `pydantic` (MIT), and `psycopg[binary]` (LGPL-3.0,
  whose binary wheel also bundles libpq under the PostgreSQL License).

See [NOTICE](NOTICE) for the full upstream attributions.

## License

This image is licensed under the GNU Affero General Public License v3.0
(AGPL-3.0-only), consistent with the percona-cd-platform repository (see
[LICENSE](LICENSE)). The image is primarily Percona's own code; the third-party
runtime and dependencies above remain under their own (permissive and LGPL)
licenses, which are compatible with redistribution alongside this AGPL-3.0 work.

## Build (multi-arch in CI)

EKS nodes are arm64 Graviton, so CI builds this multi-arch on push to `main` and
pushes to ECR under the `percona-cd/` namespace:

```
aws ecr get-login-password --region us-east-1 \
  | docker login --username AWS --password-stdin <account>.dkr.ecr.us-east-1.amazonaws.com

docker buildx build --platform linux/amd64,linux/arm64 \
  -t <account>.dkr.ecr.us-east-1.amazonaws.com/percona-cd/mtr-ingest:<tag> \
  --push images/mtr-ingest
```

## Deploy

Run by the `mtr-ingest` CronJob in the `resources/addons/mtr` addon (ArgoCD-managed,
percona-ci-platform EKS, us-east-1). The container runs unprivileged
(`runAsUser 65532`, matching the CronJob `securityContext`) with `mtr-backfill`
as the entrypoint.
