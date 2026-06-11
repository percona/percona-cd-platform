# scripts/

Operational helpers for the EKS-hosted CI platform.

Read-only by default. Most expect `AWS_PROFILE=percona-dev-admin` and a
kubeconfig with the `percona-ci-platform` context.

## Master-facing

[`check-master-spot-readiness.sh`](check-master-spot-readiness.sh)
Spot-interrupt readiness audit for a Jenkins master.

- SpotFleet has Capacity Rebalancing + pinned to `$Latest`
- SSM agent online, cloud-init done
- `crond` active, `terminate-check` cron installed and firing
- `jenkins-graceful-stop.sh` present with `flock` guard, `jq` available
- JVM has the rehydrate flag (eks_observability profile)
- Secrets Manager fetch + api-admin auth probe from loopback

Exits non-zero if anything is missing.

[`runbook.py`](runbook.py)
Gated runbook automations behind `just runbook`. One subcommand per
common operation: `template-change <inst>` is fully automated (clean
origin/main gate, plan-scope gate allowing only that master's
init-config S3 objects, apply, live evaluate via the jenkins CLI),
the rest are guided step-runners with per-step confirmation.

[`check-master-alloy-mimir.py`](check-master-alloy-mimir.py)
Verify every active master ships metrics to Mimir via Alloy. Enumerates the
master set dynamically from repo source-of-truth (k8s instances dir,
`master-*.tf` with/without the jenkins-master module -> k8s / tf-managed /
cf-managed) and reads per-master freshness of
`hetzner_api_rate_limit_remaining` from the in-cluster Mimir query-frontend.
`--max-age <s>`, `--json`; STALE/MISSING exits non-zero. Run via
`just check-master-alloy`.

[`check-master-ingest.sh`](check-master-ingest.sh)
Per-master Mimir + Loki ingest probe via in-cluster query-frontends.
Reports series count, sample freshness, metric cardinality, Loki line count.
Use for fleet-wide observability spot-checks without SSH. The master list is
hardcoded (`EXPECTED_MASTERS` in the script); `check-master-alloy-mimir.py`
derives it dynamically — pair them.

[`verify-observability.sh`](verify-observability.sh)
Per-master end-to-end LGTM push pipeline walk: master-side Alloy systemd,
ALB + bearer, alloy-gateway pods, Mimir distributor + canary, Loki ring +
canary, Grafana. Pair with `check-master-ingest.sh` for fleet-wide.

[`check-uptime-queries.py`](check-uptime-queries.py)
Validate every jenkins-uptime PromQL query against Mimir (ADR 0031): the
PrometheusRule recording-rule exprs (live from the cluster when deployed,
else rendered via `helm template`) plus every Jenkins Uptime dashboard
panel expr and its template-variable query. Auto-detects pre-deploy
(addon series may be empty, source series must exist) vs post-deploy
(everything must return series). Parse errors or unexpectedly empty
results exit non-zero.

## Cluster-facing

[`verify-karpenter.sh`](verify-karpenter.sh)
Before / during / after validation of Karpenter scale-up, scale-down, and
disruption behavior. Phase-based (`--phase scaleup-small | scaledown-empty
| all | ...`). Fixtures in [`karpenter-tests/`](karpenter-tests/).

[`verify-lgtm-cutover.sh`](verify-lgtm-cutover.sh)
Cluster-side LGTM-only migration validation: ruler sync, ALB bearer gate,
worker provisioning telemetry, dashboards, kps removal. The `worker` phase
triggers a real Jenkins build.

## Inventory

[`check_versions.py`](check_versions.py)
Version drift check for pinned tools, charts, AWS CLI. Run before bumping
`terraform/versions.tf` or addon chart pins.

## Master-side install

[`install-master-observability.sh`](install-master-observability.sh)
Bootstrapped on each EC2 Jenkins master from the TF user-data (SHA-pinned).
Installs amazon-ssm-agent + Grafana Alloy with an `ExecStartPre` that
fetches the alloy-gateway bearer token from AWS Secrets Manager. Idempotent.
Not invoked locally; lives here so platform changes to the gateway and the
master-side installer land in one diff.

## Conventions

- **Exit codes:** `0` all green, `1` at least one FAIL, `2` precondition missing.
- **Output:** PASS / FAIL / SKIP rows per stage, summary at end. ANSI on TTY.
- **Master argument:** shortname (`ps3`, `pxb`, ...) or instance id (`i-...`).
