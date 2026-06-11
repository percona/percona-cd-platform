"""Stage-row framework shared by the verifier subcommands.

One implementation of the PASS/FAIL/SKIP row conventions that the four bash
verifiers each carried a private copy of (scripts/README.md documents the
contract): two-space indented rows, ANSI color only on a TTY, `== title ==`
sections, missing-tool exit 2, stage-failure exit 1, all-pass exit 0.
"""

from __future__ import annotations

import shutil
import sys
from collections.abc import Sequence

from cdpctl._repo import die


def _tty() -> bool:
    return sys.stdout.isatty()


def green(s: str) -> str:
    return f"\033[32m{s}\033[0m" if _tty() else s


def red(s: str) -> str:
    return f"\033[31m{s}\033[0m" if _tty() else s


def gray(s: str) -> str:
    return f"\033[90m{s}\033[0m" if _tty() else s


def section(title: str) -> None:
    print(f"\n== {title} ==")


def require(*tools: str) -> None:
    """Missing prerequisite tooling is exit 2, before any stage runs."""
    for t in tools:
        if shutil.which(t) is None:
            die(f"missing required tool: {t}", 2)


class Stages:
    """Counters plus the result log behind the row helpers.

    `results` keeps (status, label, detail) tuples in emit order so callers
    can re-print failures in a summary or render a JSON envelope.
    """

    def __init__(self, quiet: bool = False) -> None:
        """quiet=True records results without printing rows (--json / --llm runs)."""
        self.quiet = quiet
        self.results: list[tuple[str, str, str]] = []

    def section(self, title: str) -> None:
        if not self.quiet:
            section(title)

    def echo(self, *args, **kw) -> None:
        """print() gated on human mode."""
        if not self.quiet:
            print(*args, **kw)

    def ok(self, label: str, detail: str = "") -> None:
        if not self.quiet:
            print(f"  {green('PASS')} {label} {gray(detail)}")
        self.results.append(("PASS", label, detail))

    def fail(self, label: str, detail: str = "") -> None:
        if not self.quiet:
            print(f"  {red('FAIL')} {label} {detail}")
        self.results.append(("FAIL", label, detail))

    def skip(self, label: str, detail: str = "") -> None:
        if not self.quiet:
            print(f"  {gray('SKIP')} {label} {gray(detail)}")
        self.results.append(("SKIP", label, detail))

    def count(self, status: str) -> int:
        return sum(1 for s, _, _ in self.results if s == status)

    @property
    def passed(self) -> int:
        return self.count("PASS")

    @property
    def failed(self) -> int:
        return self.count("FAIL")

    @property
    def skipped(self) -> int:
        return self.count("SKIP")

    def reprint_issues(self) -> None:
        """The FAIL/SKIP rows again, for the end-of-run summary block."""
        for status, label, detail in self.results:
            if status == "FAIL":
                print(f"  {red('FAIL')} {label} {detail}")
            elif status == "SKIP":
                print(f"  {gray('SKIP')} {label} {gray(detail)}")

    def exit_code(self) -> int:
        return 1 if self.failed else 0

    def envelope(self, **context) -> dict:
        """The standard --json shape for a verifier run."""
        return {
            **context,
            "pass": self.passed,
            "fail": self.failed,
            "skip": self.skipped,
            "results": [
                {"status": s, "label": label, "detail": detail} for s, label, detail in self.results
            ],
        }

    def emit_llm(self) -> None:
        """Pipe-delimited rows: status|label|detail (one line per check)."""
        for status, label, detail in self.results:
            print(f"{status}|{label}|{detail}")


def render_table(headers: Sequence[str], rows: Sequence[Sequence[object]]) -> str:
    """kubectl-style output: UPPERCASE headers, two-space gutters, no borders."""
    cells = [[("" if c is None else str(c)) for c in row] for row in rows]
    widths = [
        max(len(h), *(len(r[i]) for r in cells)) if cells else len(h) for i, h in enumerate(headers)
    ]
    lines = ["  ".join(h.upper().ljust(widths[i]) for i, h in enumerate(headers)).rstrip()]
    for r in cells:
        lines.append("  ".join(r[i].ljust(widths[i]) for i in range(len(headers))).rstrip())
    return "\n".join(lines)


def emit_rows(
    headers: Sequence[str],
    rows: Sequence[Sequence[object]],
    mode: str,
    json_payload: dict | None = None,
) -> None:
    """One table, three modes: human (kubectl-style), llm (pipe-delimited), json.

    `mode` is "human" | "llm" | "json". The json payload defaults to
    [{header: value}] built from the rows; pass json_payload for a richer
    envelope.
    """
    import json as _json

    if mode == "json":
        payload = json_payload
        if payload is None:
            keys = [h.lower() for h in headers]
            payload = {"rows": [dict(zip(keys, r, strict=False)) for r in rows]}
        print(_json.dumps(payload, indent=2, default=str))
    elif mode == "llm":
        for r in rows:
            print("|".join("" if c is None else str(c) for c in r))
    else:
        print(render_table(headers, rows))


def add_output_flags(ap) -> None:
    """The standard --json / --llm pair (mutually exclusive)."""
    g = ap.add_mutually_exclusive_group()
    g.add_argument("--json", action="store_true", help="structured JSON output")
    g.add_argument(
        "--llm", action="store_true", help="pipe-delimited output for LLM/script consumption"
    )


def output_mode(args) -> str:
    if getattr(args, "json", False):
        return "json"
    if getattr(args, "llm", False):
        return "llm"
    return "human"
