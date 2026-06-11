# src/cdpctl/ conventions

The central operational CLI ([ADR 0032](../../docs/adr/0032-central-python-cli-package.md)).
The justfile dispatches here; `scripts/` is frozen by the allowlist gate in
`conventions.py`, so ALL new automation lands as a subcommand in this package.

## Adding a subcommand

1. One module per subcommand, exposing `main(argv: list[str] | None = None) -> int`
   with `argparse` and `prog="cdpctl <name>"`.
2. Register it in `cli.py` `COMMANDS` (module path + one-line summary). Imports
   are LAZY by design: `conventions` must keep running on a bare interpreter
   (`PYTHONPATH=src python3 -m cdpctl conventions` in CI), so neither `cli.py`
   nor `_repo.py` may import third-party packages at module level
   (`load_yaml` imports yaml inside the function for this reason).
3. No module-level path constants. Resolve paths inside functions via
   `_repo.repo_root()` (cwd git toplevel first, then the install location).
4. Wrap it in a justfile recipe (`uv run --locked cdpctl <name> {{ARGS}}`),
   add the row to `scripts/README.md`, and add `_require-aws-profile` to the
   recipe when the tool calls AWS.

## Shared helpers

- `_repo.py`: `repo_root()`, `die(msg, code)`, `http_json()`, `load_yaml()`.
  Nothing else belongs there.
- `_stage.py`: verifier framework (PASS/FAIL/SKIP rows, sections, `require`)
  and the output layer (`render_table`, `emit_rows`, `add_output_flags`,
  `output_mode`, `Stages(quiet=...)`, `envelope()`, `emit_llm()`).

## Output standard

- Row tools: kubectl-style table by default (`_stage.emit_rows`), plus
  `--json` (structured envelope) and `--llm` (pipe-delimited) via
  `_stage.add_output_flags`.
- Verifiers: human stage rows by default; in `--json`/`--llm` construct
  `Stages(quiet=True)` so stdout carries ONLY the final payload.
- Exit codes: 0 clean, 1 failures, 2 missing tools or bad usage. Special:
  `runbook` gates exit 3 (`GATE FAILED`), `fork-locks --check` exits 3 on an
  available bump. Keep these stable; the justfile and CI depend on them.

## Rules that have bitten before

- `uv run --locked` everywhere in repo automation; a plain `uv run` can
  rewrite `uv.lock` and dirty the tree mid-gate. Dependency bumps are an
  explicit `uv lock` in a PR.
- Never bake org-specific values (AWS profile names, account IDs) into code,
  defaults, or help text. Read `AWS_PROFILE` from the environment and fail
  closed (`die("AWS_PROFILE must be exported", 2)`) when it is required.
- Subprocesses take list argv (never `shell=True` with interpolation); aws,
  kubectl, helm, ssh stay subprocess; HTTP goes through urllib/`http_json`.
- Prefer repo source-of-truth over hardcoded inventory (see
  `alloy.enumerate_masters`, shared by `ingest`): hardcoded master lists rot.

## Gates

`just cdp-lint` (ruff format --check, ruff check, ty) and `just cdp-test`
(pytest, pure functions only, the repo itself as fixture) run in `just ci`
and the CI `cdpctl` job. Tests live in `tests/`; subprocess/network mocking
is out of scope by convention.
