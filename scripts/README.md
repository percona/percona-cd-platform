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

Exits non-zero if anything is missing. PS-11173.

[`check-master-ingest.sh`](check-master-ingest.sh)
Per-master Mimir + Loki ingest probe via in-cluster query-frontends.
Reports series count, sample freshness, metric cardinality, Loki line count.
Use for fleet-wide observability spot-checks without SSH.

[`verify-observability.sh`](verify-observability.sh)
Per-master end-to-end LGTM push pipeline walk: master-side Alloy systemd,
ALB + bearer, alloy-gateway pods, Mimir distributor + canary, Loki ring +
canary, Grafana. Pair with `check-master-ingest.sh` for fleet-wide.

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

## Conventions

- **Exit codes:** `0` all green, `1` at least one FAIL, `2` precondition missing.
- **Output:** PASS / FAIL / SKIP rows per stage, summary at end. ANSI on TTY.
- **Master argument:** shortname (`ps3`, `pxb`, ...) or instance id (`i-...`).
