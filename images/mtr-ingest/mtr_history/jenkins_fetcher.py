"""Subprocess wrappers around the Rust `jenkins` CLI.

We don't re-implement the Jenkins REST client; the CLI at ~/.local/bin/jenkins
already handles auth, retries, rate-limiting, pagination, and artifact download.
"""

from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path


class JenkinsFetchError(RuntimeError):
    """Raised when the jenkins CLI exits non-zero."""


def _jenkins_bin() -> str:
    bin_path = shutil.which("jenkins")
    if not bin_path:
        raise JenkinsFetchError(
            "`jenkins` binary not found on PATH. "
            "Install via `cargo build --release && cp target/release/jenkins ~/.local/bin/`."
        )
    return bin_path


def cli_version() -> str:
    """Return the `jenkins --version` string, or 'unknown' on failure."""
    try:
        out = subprocess.run(
            [_jenkins_bin(), "--version"],
            capture_output=True,
            text=True,
            check=True,
            timeout=5,
        ).stdout.strip()
        return out or "unknown"
    except Exception:
        return "unknown"


def _run_json(cmd: list[str]) -> dict | list:
    proc = subprocess.run(cmd, capture_output=True, text=True)
    if proc.returncode != 0:
        raise JenkinsFetchError(
            f"jenkins CLI exited {proc.returncode}: {' '.join(cmd)}\nstderr:\n{proc.stderr}"
        )
    try:
        return json.loads(proc.stdout)
    except json.JSONDecodeError as e:
        raise JenkinsFetchError(
            f"jenkins CLI returned non-JSON output: {' '.join(cmd)}\nstdout[:500]:\n{proc.stdout[:500]}"
        ) from e


def fetch_history(instance: str, job: str, limit: int) -> list[dict]:
    """Return list of build dicts: [{number, result, duration, timestamp}, ...].

    Uses `jenkins history <inst>/<job> --limit N --json`.
    """
    payload = _run_json([
        _jenkins_bin(), "history", f"{instance}/{job}",
        "--limit", str(limit), "--json",
    ])
    if isinstance(payload, dict):
        return payload.get("builds", [])
    return []


def fetch_detail(instance: str, job: str, build_number: int) -> dict:
    """Return the full `jenkins detail --json` envelope for a single build."""
    result = _run_json([
        _jenkins_bin(), "detail", f"{instance}/{job}",
        "-b", str(build_number), "--json",
    ])
    if not isinstance(result, dict):
        raise JenkinsFetchError(f"unexpected shape from jenkins detail: {type(result).__name__}")
    return result


def download_junit_xmls(
    instance: str,
    job: str,
    build_number: int,
    dest_dir: Path,
) -> list[Path]:
    """Download junit_*.xml artefacts to dest_dir; return list of local paths."""
    dest_dir.mkdir(parents=True, exist_ok=True)
    proc = subprocess.run(
        [
            _jenkins_bin(), "artifacts", f"{instance}/{job}",
            "-b", str(build_number),
            "--match", "junit_*.xml",
            "--download", str(dest_dir),
            "--force",
        ],
        capture_output=True,
        text=True,
    )
    # `jenkins artifacts` returns 0 even for zero-match; failed downloads surface as non-zero.
    if proc.returncode != 0:
        raise JenkinsFetchError(
            f"jenkins artifacts failed ({proc.returncode}) for build {build_number}:\n"
            f"stderr:\n{proc.stderr}"
        )
    # CLI writes into <dest>/work/results/junit_*.xml per Jenkins artefact layout.
    # Flatten: collect all junit_*.xml recursively.
    return sorted(dest_dir.rglob("junit_*.xml"))
