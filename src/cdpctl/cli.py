"""cdpctl: the central operational CLI for percona-cd-platform.

The justfile is the human entry point and dispatches here (`just runbook` ->
`uv run --locked cdpctl runbook`). Subcommand modules import LAZILY so the
stdlib-only subcommands (conventions) run on a bare interpreter without the
project's third-party dependencies, which is how the CI tofu job invokes it:
`PYTHONPATH=src python3 -m cdpctl conventions`.
"""

from __future__ import annotations

import importlib
import sys

# name -> (module, one-line summary shown by `cdpctl --help`)
COMMANDS: dict[str, tuple[str, str]] = {
    "runbook": (
        "cdpctl.runbook",
        "gated runbook automations (template-change, master-resize, core-bump, ssh-key)",
    ),
    "alloy": ("cdpctl.alloy", "every active master ships metrics to Mimir via Alloy"),
    "clouds": ("cdpctl.clouds", "render/apply/check the agent-clouds catalog (ADR 0029)"),
    "conventions": ("cdpctl.conventions", "terraform conventions + scripts/ allowlist gate"),
    "versions": ("cdpctl.versions", "pinned versions vs upstream latest"),
    "ingest": ("cdpctl.ingest", "per-master Mimir + Loki ingest probe"),
    "spot-readiness": ("cdpctl.spot_readiness", "spot-interrupt readiness audit for a master"),
    "verify-observability": (
        "cdpctl.verify_observability",
        "per-master LGTM push-pipeline walk (deepest check)",
    ),
    "verify-karpenter": (
        "cdpctl.verify_karpenter",
        "Karpenter scale-up/down/disruption validation phases",
    ),
    "helm-render-check": (
        "cdpctl.helm_render",
        "controller chart values reach the jenkins subchart (CI gate)",
    ),
    "fork-locks": (
        "cdpctl.fork_locks",
        "auto-bump the fork plugin lock to latest .percona. releases",
    ),
}


def main(argv: list[str] | None = None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    if not argv or argv[0] in ("-h", "--help"):
        print("usage: cdpctl <subcommand> [args]\n\nsubcommands:")
        for name, (_, summary) in COMMANDS.items():
            print(f"  {name:<22} {summary}")
        print("\nThe justfile wraps these (see `just --list`); prose in scripts/README.md.")
        return 0
    cmd, rest = argv[0], argv[1:]
    if cmd not in COMMANDS:
        print(f"cdpctl: unknown subcommand {cmd!r} (run `cdpctl --help`)", file=sys.stderr)
        return 2
    mod = importlib.import_module(COMMANDS[cmd][0])
    return int(mod.main(rest) or 0)


if __name__ == "__main__":
    raise SystemExit(main())
