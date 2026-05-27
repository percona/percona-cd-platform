# 0020 — MTR test-history on a relational store (CloudNativePG), not the LGTM time-series stack

**Status:** Accepted (2026-05-27), rollout pending (PS-10541)
**Related:** [ADR 0016](0016-lgtm-only-metrics-stack.md) (LGTM-only metrics; this is the deliberate non-Mimir exception for relational data), [ADR 0010](0010-distributed-lgtm.md) (LGTM topology), [ADR 0008](0008-managed-ng-for-stateful-system-workloads.md) (stateful tier placement reused for the DB), [ADR 0012](0012-authentik-saml-oidc-bridge.md) (its bundled Bitnami Postgres shares the migration implication below)

## Context

PS-10541 asks for per-test failure history that is easy to query: sporadic failures (e.g. `log_buffered-big`), what failed in a given build, and the fail-then-pass-on-retry cases the Jenkins UI hides. A PoC (`Percona-Lab/mysql-mtr-history-dashboard`) answered this on a standalone Hetzner box: a Jenkins job parses JUnit XML into per-build JSON, exports OpenMetrics, and `promtool tsdb create-blocks` writes it into a local Prometheus that a Grafana failure-matrix dashboard reads. The box must be retired and the capability folded into this platform.

The PoC's time-series model is a poor fit, and pushing it onto the platform LGTM stack makes that worse:

1. **Historical timestamps.** Every sample is stamped at the Jenkins build start time, often weeks or months old. Mimir's distributor rejects them (`reject_old_samples` default 14d; no `out_of_order_time_window`; block-upload disabled). The only TS path that preserves the timestamps is compactor block-upload, which keeps the wrong model.
2. **Query shape.** The ticket's questions are relational GROUP BY / window queries (history of a test, flaky detection, failures in build N), not PromQL range queries.
3. **Cardinality.** ~13.7K distinct tests per platform across many build / branch / arch combinations is high label cardinality for a single-ingester Mimir.

Test history is relational event data. A SQL table makes the build timestamp an ordinary column and maps every ticket query to plain SQL.

## Decision

Store MTR test history in **PostgreSQL**, query it through Grafana's **core (OSS, free) Postgres datasource**, and ingest with an **hourly EKS CronJob**.

- **Engine: CloudNativePG** (operator chart 0.28.2 / operator 1.29.1), not the Bitnami postgresql subchart. Bitnami's free catalogue was frozen to `bitnamilegacy` (no security patches) after the Broadcom 2025 change, so pinning a Bitnami chart pulls an unpatched image. CNPG is CNCF, actively maintained, ships official Postgres images, and is declarative (a `Cluster` CR with optional S3 PITR). It also gives the platform a maintained target to migrate Authentik's bundled Bitnami Postgres (ADR 0012) onto later.
- **Single instance**, `obs-state` tier (single-AZ; EBS is per-AZ), `gp3-monitoring-1a-retain`. No HA or heavy backups: the data is fully rebuildable from Jenkins (the CronJob re-backfills), so a lost volume costs a re-ingest, not data.
- **Schema** (`builds` + `test_results`, with `test_runs` and `test_flakiness` views). Test identity is `(suite, name, run_context)` with `worker`/`big` as attributes, matching the PoC's JUnit parser. A read-only `mtr_reader` CNPG managed role serves Grafana; the ingest CronJob writes as the app owner.
- **Ingest**: an EKS CronJob (cloned from the `jenkins-endpoint-reconciler` pattern) runtime-installs the PoC package from its public source archive and runs `init-db` (idempotent schema + grants), `pg-skip-list`, `fetch-rest` (all build results), `ingest-pg` (UPSERT, one transaction per build). Idempotent: a re-ingest replaces a build's rows exactly. Jenkins REST auth and the role passwords come from AWS Secrets Manager via ESO (`percona-ci-platform/mtr/config`), since the cluster cannot read ps80's Jenkins credential store.
- **Datasource**: a `postgres` datasource (`uid: mtr-postgres`) reading the CNPG `-r` service as `mtr_reader`. No plugin install and no license (core OSS); the "MySQL datasource needs a license" wall seen elsewhere was PMM / Amazon Managed Grafana, not `grafana/grafana` OSS.

## Consequences

- One new operator (CNPG) and its CRDs. The `Cluster` CR carries `SkipDryRunOnMissingResource=true` so ArgoCD's first sync does not fail before the operator app installs the CRD; automated self-heal then applies it (the same eventual consistency the `prometheus-operator-crds` consumers rely on).
- **ClickHouse is the documented scale-up.** Postgres is right to low tens of millions of rows (100 builds = ~1M rows, measured). If ingest expands to all products and all builds, migrate to ClickHouse; the column set is designed to map 1:1.
- **Loki-reuse was rejected**: it would force `reject_old_samples` off on a shared production component and still answers the flaky/history queries poorly.
- The Grafana **dashboard JSON port** (PromQL failure-matrix to SQL) is deferred to live-Grafana iteration at deploy. **Within-build retry capture** (the fail-then-pass Jenkins hides inside one build) is a follow-up needing an `attempt` column and a JUnit-parser change; v1 detects cross-build flapping via the `test_flakiness` view.
- The CronJob installs from a mutable branch tarball until the PoC PR merges; pin to a tag/SHA (or build an image) afterward. `ingest-pg` exits non-zero on any per-build error so a partial tick is not reported green.

## Codex review (2026-05-27)

A read-only codex review on the authored changes flagged, and this design addresses: the CRD-before-CR dry-run risk (SkipDryRunOnMissingResource), the mutable-tarball install (pin post-merge), silent partial ingest (`ingest-pg` now exits non-zero), and an unbounded `pg-skip-list` argv after a large backfill (bounded via `--limit`). Packaging (the wheel ships `sql/schema.sql` and the `mtr-backfill` console script) was verified by building the wheel.

## Alternatives considered

- **Mimir compactor block-upload** (keep time-series): preserves historical timestamps but keeps the wrong model (cardinality, time-picker queries) and needs an experimental feature enabled. Rejected.
- **RDS**: managed, but adds a subnet group, a security group, and an ARN/host (cloud-shaped values vs the public-repo no-ARNs rule) for a rebuildable internal DB. Deferred to the documented CNPG-to-RDS path if scale demands it.
- **Infinity over S3 JSON**: lowest-ops, reuses the per-build JSON, but precomputed views only and scales poorly. A viable fast interim, not the target.
