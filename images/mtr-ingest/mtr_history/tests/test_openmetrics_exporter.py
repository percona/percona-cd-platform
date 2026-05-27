"""Tests for the OpenMetrics exporter."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from mtr_history.openmetrics_exporter import (
    Sample,
    _build_label,
    _normalize_os,
    _ts_seconds,
    _worker_num,
    _worker_slug,
    build_openmetrics_bundle,
    iter_samples,
    merge_openmetrics_files,
    write_openmetrics,
)

# --- small unit tests -------------------------------------------------------

def test_build_label_padding() -> None:
    assert _build_label(1) == "00001"
    assert _build_label(1558) == "01558"
    assert _build_label(99999) == "99999"


def test_normalize_os() -> None:
    assert _normalize_os("oraclelinux:10") == "oraclelinux-10"
    assert _normalize_os("ubuntu:noble") == "ubuntu-noble"


def test_ts_seconds_parses_z() -> None:
    # 2026-03-26T11:10:35Z → 1774523435.0 (UTC)
    assert _ts_seconds("2026-03-26T11:10:35Z") == pytest.approx(1774523435.0)


def test_worker_slug_and_num() -> None:
    assert _worker_slug("WORKER_2", False, "regular") == "WORKER_2"
    assert _worker_slug("WORKER_2", True, "regular") == "WORKER_2-big"
    assert _worker_slug("WORKER_1", False, "ci_fs") == "ci_fs"
    assert _worker_num("WORKER_2", "regular") == "2"
    assert _worker_num("WORKER_1", "ci_fs") == ""


# --- iter_samples against a minimal build JSON ------------------------------

_MIN_BUILD = {
    "build_number": 1558,
    "url": "https://ps80.cd.percona.com/job/percona-server-8.0-pipeline-parallel-mtr/1558/",
    "timestamp": "2026-03-26T11:10:35Z",
    "duration_ms": 5885562,
    "result": "UNSTABLE",
    "display_name": "#1558",
    "platform": {"docker_os": "oraclelinux:10", "arch": "x86_64", "cmake_build_type": "Debug"},
    "source": {"fork": "dlenev", "repo": "https://github.com/dlenev/percona-server",
               "branch": "ps-8.0-10448", "sha": None},
    "cause": {"kind": "Manual", "user": "Dmitry Lenev", "description": "Started by user Dmitry Lenev"},
    "scm": [],
    "parameters": {},
    "summary": {"pass": 1, "fail": 1, "skip": 1, "total": 3, "duration_s": 10.5},
    "tests": [
        {"suite": "audit_log", "name": "audit_log_charset", "run_context": "regular",
         "worker": "WORKER_2", "big": False, "status": "fail", "time_s": 0.0,
         "failure_message": "Test failed"},
        {"suite": "audit_log", "name": "audit_log_filter", "run_context": "regular",
         "worker": "WORKER_2", "big": False, "status": "pass", "time_s": 8.32,
         "failure_message": None},
        {"suite": "innodb", "name": "innodb_bug60196", "run_context": "ci_fs",
         "worker": "WORKER_1", "big": False, "status": "skip", "time_s": 0.0,
         "failure_message": None},
    ],
    "backfill_meta": {"schema_version": 1, "backfill_ts": "2026-04-15T14:00:00Z",
                      "jenkins_cli_version": "test", "junit_xml_files": [], "warnings": []},
}


def test_iter_samples_counts() -> None:
    samples = list(iter_samples(_MIN_BUILD))
    # 1 build_info + 3 build_summary_total + 3×(status+duration) + 1 failure_info = 11
    assert len(samples) == 11
    metrics = {s.metric for s in samples}
    assert metrics == {
        "mtr_build_info",
        "mtr_build_summary_total",
        "mtr_test_failure_info",
        "mtr_test_status",
        "mtr_test_duration_seconds",
    }


def test_iter_samples_enum_values() -> None:
    samples = list(iter_samples(_MIN_BUILD))
    # Find the mtr_test_status for audit_log_charset — should be 0 (fail).
    charset = next(
        s for s in samples
        if s.metric == "mtr_test_status"
        and s.labels.get("testname") == "audit_log_charset"
    )
    assert charset.value == 0.0
    filter_pass = next(
        s for s in samples
        if s.metric == "mtr_test_status"
        and s.labels.get("testname") == "audit_log_filter"
    )
    assert filter_pass.value == 1.0


def test_iter_samples_labels_include_worker_slug_and_num() -> None:
    samples = list(iter_samples(_MIN_BUILD))
    charset = next(
        s for s in samples
        if s.metric == "mtr_test_status"
        and s.labels.get("testname") == "audit_log_charset"
    )
    assert charset.labels["worker_slug"] == "WORKER_2"
    assert charset.labels["worker_num"] == "2"
    assert charset.labels["build"] == "01558"
    assert charset.labels["platform_os"] == "oraclelinux-10"

    cifs = next(
        s for s in samples
        if s.metric == "mtr_test_status"
        and s.labels.get("testname") == "innodb_bug60196"
    )
    assert cifs.labels["worker_slug"] == "ci_fs"
    assert cifs.labels["worker_num"] == ""
    assert cifs.labels["run_context"] == "ci_fs"


def test_iter_samples_failure_info() -> None:
    samples = list(iter_samples(_MIN_BUILD))
    fi = [s for s in samples if s.metric == "mtr_test_failure_info"]
    # Only 1 test is failed (audit_log_charset), so exactly 1 failure_info sample.
    assert len(fi) == 1
    assert fi[0].labels["testname"] == "audit_log_charset"
    assert fi[0].labels["failure_msg"] == "Test failed"
    assert fi[0].value == 1.0
    # Passing and skipped tests should NOT have failure_info.
    names = {s.labels.get("testname") for s in fi}
    assert "audit_log_filter" not in names
    assert "innodb_bug60196" not in names


def test_iter_samples_build_info() -> None:
    samples = list(iter_samples(_MIN_BUILD))
    info = next(s for s in samples if s.metric == "mtr_build_info")
    assert info.value == 1.0
    assert info.labels["build_url"].endswith("/1558/")
    assert info.labels["result"] == "UNSTABLE"
    assert info.labels["cause_user"] == "Dmitry Lenev"
    assert info.labels["fork"] == "dlenev"


# --- file output -----------------------------------------------------------

def test_write_openmetrics_roundtrip(tmp_path: Path) -> None:
    out = tmp_path / "b.openmetrics.txt"
    n = write_openmetrics(iter_samples(_MIN_BUILD), out)
    assert n == 11
    text = out.read_text()
    assert text.endswith("# EOF\n")
    # HELP + TYPE headers present for each metric family.
    for metric in ("mtr_build_info", "mtr_build_summary_total", "mtr_test_failure_info", "mtr_test_status", "mtr_test_duration_seconds"):
        assert f"# HELP {metric}" in text
        assert f"# TYPE {metric}" in text
    # Timestamp is the Jenkins build timestamp, not "now".
    assert "1774523435.000" in text


def test_merge_openmetrics_sorts_globally(tmp_path: Path) -> None:
    # Two mini-builds; confirm merged file has sorted lines and single EOF.
    a_dir = tmp_path / "by-build"
    write_openmetrics(iter_samples(_MIN_BUILD), a_dir / "01558.openmetrics.txt")

    second_build = dict(_MIN_BUILD)
    second_build["build_number"] = 1559
    second_build["url"] = _MIN_BUILD["url"].replace("1558", "1559")
    write_openmetrics(iter_samples(second_build), a_dir / "01559.openmetrics.txt")

    merged = tmp_path / "merged.txt"
    n = merge_openmetrics_files(a_dir, merged)
    assert n == 22  # 11 samples × 2 builds
    lines = merged.read_text().splitlines()
    sample_lines = [l for l in lines if not l.startswith("#") and l]
    assert sample_lines == sorted(sample_lines)


def test_build_openmetrics_bundle_writes_per_build_files(tmp_path: Path) -> None:
    json_dir = tmp_path / "builds"
    json_dir.mkdir()
    (json_dir / "ps80_foo_1558.json").write_text(json.dumps(_MIN_BUILD))
    report = build_openmetrics_bundle(json_dir, tmp_path / "om")
    assert report["builds_processed"] == 1
    assert (tmp_path / "om" / "01558.openmetrics.txt").exists()
