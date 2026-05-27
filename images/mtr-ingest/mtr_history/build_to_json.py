"""Orchestrate one-build pipeline: jenkins detail + junit XMLs → per-build JSON."""

from __future__ import annotations

import json
import shutil
from datetime import datetime, timezone
from pathlib import Path

from . import jenkins_fetcher, jenkins_rest_fetcher, junit_parser
from .schema import (
    SCHEMA_VERSION,
    BackfillMeta,
    Build,
    Cause,
    Platform,
    ScmEntry,
    Source,
    Summary,
    TestResult,
)


def _json_filename(instance: str, job: str, build_number: int) -> str:
    return f"{instance}_{job}_{build_number}.json"


def _iso_now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _iso_from_ms(ms: int | None) -> str:
    if not ms:
        return _iso_now()
    return datetime.fromtimestamp(ms / 1000.0, tz=timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


# --- extraction helpers -----------------------------------------------------

# Parameters to drop unconditionally: per-worker suite overrides and
# noise that bloats the JSON without adding analytical value.
_PARAM_DROPLIST = {
    *{f"WORKER_{n}_MTR_SUITES" for n in range(1, 9)},
    "LAUNCHER_USER_ID",
    "CUSTOM_BUILD_NAME",
}


def extract_platform(detail: dict) -> Platform:
    params = {p["name"]: p.get("value", "") for p in detail.get("parameters", [])}
    return Platform(
        docker_os=params.get("DOCKER_OS", "unknown"),
        arch=params.get("ARCH", "x86_64"),
        cmake_build_type=params.get("CMAKE_BUILD_TYPE", "unknown"),
    )


def extract_source(detail: dict) -> Source:
    src = detail.get("source") or {}
    # Fallback: derive fork from the GIT_REPO parameter if jenkins CLI didn't.
    repo = src.get("repo") or ""
    fork = src.get("fork")
    if not fork and repo:
        # e.g. "https://github.com/dlenev/percona-server" → "dlenev"
        stripped = repo.rstrip("/").removesuffix(".git")
        parts = stripped.split("/")
        if len(parts) >= 2:
            fork = parts[-2]
    return Source(
        fork=fork,
        repo=repo,
        branch=src.get("branch") or "",
        sha=src.get("sha"),
    )


def extract_cause(detail: dict) -> Cause:
    c = detail.get("cause") or {}
    return Cause(
        kind=c.get("kind") or "Unknown",
        user=c.get("user"),
        description=c.get("description") or "",
    )


def extract_scm(detail: dict) -> list[ScmEntry]:
    out: list[ScmEntry] = []
    for s in detail.get("scm", []) or []:
        if not s.get("remote"):
            continue
        out.append(ScmEntry(
            remote=s["remote"],
            branch=s.get("branch", ""),
            sha=s.get("sha", ""),
        ))
    return out


def extract_parameters(detail: dict) -> dict[str, str]:
    out: dict[str, str] = {}
    for p in detail.get("parameters", []) or []:
        name = p.get("name")
        value = p.get("value")
        if not name or name in _PARAM_DROPLIST:
            continue
        if value is None or value == "":
            continue
        out[name] = str(value)
    return out


def summary_from_records(records: list[junit_parser.RawTestRecord]) -> Summary:
    pass_ = sum(1 for r in records if r.status == "pass")
    fail = sum(1 for r in records if r.status == "fail")
    skip = sum(1 for r in records if r.status == "skip")
    dur = sum(r.time_s for r in records)
    return Summary.model_validate({
        "pass": pass_,
        "fail": fail,
        "skip": skip,
        "total": pass_ + fail + skip,
        "duration_s": round(dur, 3),
    })


def test_records_to_schema(records: list[junit_parser.RawTestRecord]) -> list[TestResult]:
    out = [
        TestResult(
            suite=r.suite,
            name=r.name,
            run_context=r.run_context,
            worker=r.worker or "",
            big=r.big,
            status=r.status,  # type: ignore[arg-type]
            time_s=r.time_s,
            failure_message=r.failure_message,
        )
        for r in records
    ]
    # Stable sort for reproducible JSON diffs.
    out.sort(key=lambda t: (t.suite, t.name, t.run_context, t.worker, t.big))
    return out


# --- tombstone for builds that couldn't be fetched --------------------------

def _tombstone(
    instance: str,
    job: str,
    build_number: int,
    error: str,
    history_entry: dict | None,
) -> Build:
    ts = history_entry.get("timestamp") if history_entry else None
    # timestamp may be ISO string or ms-epoch; normalize.
    if isinstance(ts, (int, float)):
        ts_iso = _iso_from_ms(int(ts))
    elif isinstance(ts, str) and ts:
        ts_iso = ts
    else:
        ts_iso = _iso_now()
    result = (history_entry or {}).get("result") or "FETCH_ERROR"
    return Build(
        build_number=build_number,
        url=f"https://{instance}.cd.percona.com/job/{job}/{build_number}/",
        timestamp=ts_iso,
        duration_ms=int((history_entry or {}).get("duration_ms") or 0),
        result=result,
        display_name=f"{build_number} (tombstone)",
        platform=Platform(docker_os="unknown", arch="unknown", cmake_build_type="unknown"),
        source=Source(fork=None, repo="", branch="", sha=None),
        cause=Cause(kind="Unknown", user=None, description=""),
        scm=[],
        parameters={},
        summary=Summary.model_validate({"pass": 0, "fail": 0, "skip": 0, "total": 0, "duration_s": 0.0}),
        tests=[],
        backfill_meta=BackfillMeta(
            schema_version=SCHEMA_VERSION,
            backfill_ts=_iso_now(),
            jenkins_cli_version=jenkins_fetcher.cli_version(),
            junit_xml_files=[],
            warnings=[],
            error=error[:2048],
        ),
    )


# --- main entry point -------------------------------------------------------

def process_build(
    instance: str,
    job: str,
    build_number: int,
    builds_dir: Path,
    force: bool = False,
    keep_xml: bool = False,
    history_entry: dict | None = None,
) -> Path | None:
    """Fetch+parse+write the per-build JSON.

    Returns the written path, or None if skipped via idempotency check.
    Never raises for transient Jenkins errors — writes a FETCH_ERROR tombstone.
    """
    builds_dir.mkdir(parents=True, exist_ok=True)
    out_path = builds_dir / _json_filename(instance, job, build_number)

    # Idempotency.
    if out_path.exists() and not force:
        try:
            existing = json.loads(out_path.read_text())
            if existing.get("backfill_meta", {}).get("schema_version") == SCHEMA_VERSION:
                return None
        except (OSError, json.JSONDecodeError):
            pass  # corrupt file — re-fetch

    # Fetch detail.
    try:
        detail = jenkins_fetcher.fetch_detail(instance, job, build_number)
    except jenkins_fetcher.JenkinsFetchError as e:
        build = _tombstone(instance, job, build_number, f"fetch_detail failed: {e}", history_entry)
        _write_build(out_path, build)
        return out_path

    # Download junit XMLs (may be 0 for FAILURE/ABORTED).
    xml_dir = builds_dir / "xml" / str(build_number)
    try:
        xml_files = jenkins_fetcher.download_junit_xmls(instance, job, build_number, xml_dir)
    except jenkins_fetcher.JenkinsFetchError as e:
        build = _tombstone(
            instance, job, build_number,
            f"download_junit_xmls failed: {e}", history_entry,
        )
        _write_build(out_path, build)
        return out_path

    # Parse + merge.
    if xml_files:
        records, warnings = junit_parser.parse_all_junit_files(xml_dir)
    else:
        records, warnings = [], []

    # Build the JSON.
    build = Build(
        build_number=detail.get("build") or build_number,
        url=detail.get("url") or f"https://{instance}.cd.percona.com/job/{job}/{build_number}/",
        timestamp=_iso_from_ms(detail.get("timestamp_ms")),
        duration_ms=int(detail.get("duration_ms") or 0),
        result=detail.get("result") or "UNKNOWN",
        display_name=(detail.get("display_name") or "").strip() or f"#{build_number}",
        platform=extract_platform(detail),
        source=extract_source(detail),
        cause=extract_cause(detail),
        scm=extract_scm(detail),
        parameters=extract_parameters(detail),
        summary=summary_from_records(records),
        tests=test_records_to_schema(records),
        backfill_meta=BackfillMeta(
            schema_version=SCHEMA_VERSION,
            backfill_ts=_iso_now(),
            jenkins_cli_version=jenkins_fetcher.cli_version(),
            junit_xml_files=sorted(p.name for p in xml_files),
            warnings=warnings,
            error=None,
        ),
    )

    _write_build(out_path, build)

    if not keep_xml:
        shutil.rmtree(xml_dir, ignore_errors=True)

    return out_path


def process_build_rest(
    base_url: str,
    job: str,
    build_number: int,
    builds_dir: Path,
    username: str | None = None,
    token: str | None = None,
    force: bool = False,
    keep_xml: bool = False,
    history_entry: dict | None = None,
) -> Path | None:
    """Like process_build() but uses the REST API fetcher instead of the Rust CLI."""
    builds_dir.mkdir(parents=True, exist_ok=True)
    # Derive instance from base_url for the filename.
    instance = base_url.split("://", 1)[-1].split(".")[0] if "://" in base_url else "unknown"
    out_path = builds_dir / _json_filename(instance, job, build_number)

    if out_path.exists() and not force:
        try:
            existing = json.loads(out_path.read_text())
            if existing.get("backfill_meta", {}).get("schema_version") == SCHEMA_VERSION:
                return None
        except (OSError, json.JSONDecodeError):
            pass

    try:
        detail = jenkins_rest_fetcher.fetch_detail(base_url, job, build_number, username, token)
    except jenkins_rest_fetcher.JenkinsFetchError as e:
        build = _tombstone(instance, job, build_number, f"fetch_detail failed: {e}", history_entry)
        _write_build(out_path, build)
        return out_path

    xml_dir = builds_dir / "xml" / str(build_number)
    try:
        xml_files = jenkins_rest_fetcher.download_junit_xmls(
            base_url, job, build_number, xml_dir, username, token,
        )
    except jenkins_rest_fetcher.JenkinsFetchError as e:
        build = _tombstone(
            instance, job, build_number,
            f"download_junit_xmls failed: {e}", history_entry,
        )
        _write_build(out_path, build)
        return out_path

    if xml_files:
        records, warnings = junit_parser.parse_all_junit_files(xml_dir)
    else:
        records, warnings = [], []

    build = Build(
        build_number=detail.get("build") or build_number,
        url=detail.get("url") or f"{base_url.rstrip('/')}/job/{job}/{build_number}/",
        timestamp=_iso_from_ms(detail.get("timestamp_ms")),
        duration_ms=int(detail.get("duration_ms") or 0),
        result=detail.get("result") or "UNKNOWN",
        display_name=(detail.get("display_name") or "").strip() or f"#{build_number}",
        platform=extract_platform(detail),
        source=extract_source(detail),
        cause=extract_cause(detail),
        scm=extract_scm(detail),
        parameters=extract_parameters(detail),
        summary=summary_from_records(records),
        tests=test_records_to_schema(records),
        backfill_meta=BackfillMeta(
            schema_version=SCHEMA_VERSION,
            backfill_ts=_iso_now(),
            jenkins_cli_version=jenkins_rest_fetcher.cli_version(),
            junit_xml_files=sorted(p.name for p in xml_files),
            warnings=warnings,
            error=None,
        ),
    )

    _write_build(out_path, build)

    if not keep_xml:
        shutil.rmtree(xml_dir, ignore_errors=True)

    return out_path


def _write_build(path: Path, build: Build) -> None:
    # Dump with pass→"pass" alias and indent for human-diffability.
    path.write_text(build.model_dump_json(by_alias=True, indent=2) + "\n")
