# mtr-ingest

Container image for the MySQL MTR test-history ingest job (PS-10541). It packages
the Percona `mtr-history` Python application and runs its `mtr-backfill` console
script: it fetches per-build MTR results from Jenkins, parses the JUnit XML into a
normalized schema, and writes the history to Postgres (and/or emits OpenMetrics)
so the Grafana failure matrix can render test history over time.

## License

Percona's code is AGPL-3.0 (see [LICENSE](LICENSE)); the `uv`/CPython base image
and the Python dependencies keep their own (permissive and LGPL) licenses. See
[NOTICE](NOTICE) for the full attribution.

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
