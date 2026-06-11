"""Stage-row framework shared by the verifier subcommands.

One implementation of the PASS/FAIL/SKIP row conventions that the four bash
verifiers each carried a private copy of (scripts/README.md documents the
contract): two-space indented rows, ANSI color only on a TTY, `== title ==`
sections, missing-tool exit 2, stage-failure exit 1, all-pass exit 0.
"""

from __future__ import annotations

import shutil
import sys

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

    def __init__(self) -> None:
        self.results: list[tuple[str, str, str]] = []

    def ok(self, label: str, detail: str = "") -> None:
        print(f"  {green('PASS')} {label} {gray(detail)}")
        self.results.append(("PASS", label, detail))

    def fail(self, label: str, detail: str = "") -> None:
        print(f"  {red('FAIL')} {label} {detail}")
        self.results.append(("FAIL", label, detail))

    def skip(self, label: str, detail: str = "") -> None:
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
