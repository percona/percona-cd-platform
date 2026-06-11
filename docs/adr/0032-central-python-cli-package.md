# 0032: Central Python CLI package (cdpctl) behind the justfile

Date: 2026-06-11

## Status

Accepted

## Context

Operational tooling had grown to 13 loose files under `scripts/` (5 Python, 8
bash) with four divergent `uv run` invocation styles in the justfile, no
lockfile, and duplicated helpers everywhere: five copies of repo-root
resolution, three subprocess-wrapper styles, two table printers in the Python
scripts, and four private copies of the PASS/FAIL/SKIP row framework in the
bash verifiers. Dependency flags (`--with pyyaml`) lived per-recipe and CI
provided the same dependencies a different way again (`pip install pyyaml`),
so the laptop and CI could legitimately disagree. Nothing stopped the next
one-off script from being added alongside.

## Decision

One uv-managed package, `cdpctl`, owns all portable operational tooling:

- `pyproject.toml` at the repo root, src layout (`src/cdpctl/`), console
  script `cdpctl`, committed `uv.lock`. Dependencies stay minimal (pyyaml,
  python-hcl2); dev toolchain is pytest, pytest-cov, ruff, ty.
- The justfile remains the human entry point and becomes a thin dispatcher:
  recipe names are unchanged and every body is `uv run --locked cdpctl ...`.
  `--locked` everywhere: invocations never re-resolve or write the lock, and
  fail loudly when pyproject and lock disagree (lock churn previously dirtied
  the runbook clean-checkout gate twice).
- Subcommands import lazily, so the stdlib-only conventions gate runs in CI
  on the bare system interpreter (`PYTHONPATH=src python3 -m cdpctl
  conventions`) with no project env.
- `scripts/` is frozen by a symmetric allowlist inside `cdpctl conventions`:
  every tracked file must be allowlisted, and every allowlist entry must
  exist. New automation lands as a subcommand or CI fails. The allowlist
  holds only `README.md`, the boot-fetched
  `install-master-observability.sh` (SHA-pinned URL contract in the master
  user-data), and the `karpenter-tests/` fixtures.
- The bash verifiers were ported, not wrapped: ingest, spot-readiness,
  verify-observability, verify-karpenter, helm-render-check and fork-locks
  are Python modules sharing one `_stage` row framework, with flags, env
  vars, output rows and exit codes preserved. jq/curl/unzip dependencies
  became stdlib json/urllib/zipfile. The finished-migration one-shot
  verifier (verify-lgtm-cutover.sh) was deleted rather than ported.

## Consequences

- Laptop and CI run identical locked dependency sets; the pyyaml-sensitive
  clouds drift gate can no longer diverge between the two.
- Gate logic is unit-testable (`tests/`, `just cdp-test` in `just ci`); the
  runbook plan-scope gate and the allowlist are covered in both directions.
- Script proliferation is now a CI failure instead of a review nitpick, and
  deleting or porting a script forces the allowlist (and the scripts/README
  map) to shrink in the same PR.
- Costs accepted: one more indirection hop (just, uv, console script), uv on
  the CI runners (SHA-pinned `astral-sh/setup-uv`), and `lambda-test` needs
  `--no-project` so the root project never leaks into the pinned Lambda test
  env.
- `ty` is in beta; the lockfile pins it, so a breaking ty release cannot
  affect CI until an explicit `uv lock` bump.
