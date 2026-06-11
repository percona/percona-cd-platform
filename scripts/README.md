# scripts/

This directory is frozen ([ADR 0032](../docs/adr/0032-central-python-cli-package.md)).
Operational automation lives in the `cdpctl` package (`src/cdpctl/`), a
uv-managed project the justfile dispatches into. The freeze is enforced by
`cdpctl conventions` in `just ci`: a new file here fails CI unless it is
allowlisted in `src/cdpctl/conventions.py`, and a stale allowlist entry fails
too.

## The dispatcher map

`just` is the human entry point; every recipe wraps `uv run --locked cdpctl
<subcommand>`. `uv.lock` is the single dependency truth (bump via `uv lock`).

| just recipe | cdpctl subcommand | module | what it does |
|---|---|---|---|
| `just runbook ...` | `runbook` | `runbook.py` | Gated runbook automations (template-change AUTOMATED, master-resize / core-bump / ssh-key GUIDED) |
| `just check-master-alloy` | `alloy` | `alloy.py` | Every active master ships metrics to Mimir via Alloy (dynamic fleet enumeration) |
| `just clouds-render-check` | `clouds check ps3` | `clouds.py` | Agent-clouds catalog render / apply / drift gate (ADR 0029) |
| `just tf-conventions` | `conventions` | `conventions.py` | Terraform conventions + the scripts/ allowlist freeze (stdlib-only) |
| `just check-versions` | `versions` | `versions.py` | Pinned versions vs upstream latest (advisory) |
| `just check-master-ingest` | `ingest` | `ingest.py` | Per-master Mimir + Loki ingest probe |
| `just check-master-spot-readiness` | `spot-readiness` | `spot_readiness.py` | Spot-interrupt readiness audit for one master |
| `just verify-observability` | `verify-observability` | `verify_observability.py` | Deepest per-master LGTM push-pipeline walk |
| `just verify-karpenter` | `verify-karpenter` | `verify_karpenter.py` | Karpenter scale-up/down/disruption phases (fixtures in `karpenter-tests/`) |
| `just helm-render` | `helm-render-check` | `helm_render.py` | Controller chart values reach the `jenkins` subchart (CI gate) |
| `just refresh-fork-locks` / `check-fork-locks` | `fork-locks [--check]` | `fork_locks.py` | Fork plugin lock auto-bump, sha256 + MANIFEST verified |
| `just check-uptime-queries` | `uptime-queries` | `uptime_queries.py` | jenkins-uptime PromQL set validated against Mimir (rules + dashboard) |

`cdpctl --help` prints the same census. `just cdpctl-install` puts `cdpctl` on
PATH as an editable uv tool. `just cdp-lint` (ruff + ty) and `just cdp-test`
(pytest) gate the package in `just ci`.

## What still lives here

| file | why it cannot move |
|---|---|
| `install-master-observability.sh` | Runs ON each EC2 master at boot, fetched via a commit-SHA-pinned raw URL in `terraform/modules/jenkins-master/user-data.sh.tftpl`. Not invoked locally. |
| `karpenter-tests/*.yaml` | kubectl fixtures consumed by `cdpctl verify-karpenter`. New fixtures are fine; they ride the directory allowlist. |

## Conventions

- Most subcommands expect `AWS_PROFILE=<your-profile>` and a kubeconfig
  with the `percona-ci-platform` context; the justfile guards the AWS ones.
- Verifiers print two-space `PASS` / `FAIL` / `SKIP` rows (ANSI only on a
  TTY) and exit 0 clean, 1 on failures, 2 on missing tools or bad usage.
- Special exits: `cdpctl runbook` gates fail with 3 (`GATE FAILED`),
  `cdpctl fork-locks --check` exits 3 when a bump is available.
- Read-only by default. The exceptions: `runbook` applies gated tofu changes,
  `clouds apply` rewrites an instance values file, `fork-locks` (without
  `--check`) rewrites the lock, `verify-karpenter` creates and deletes test
  workloads on the cluster.
