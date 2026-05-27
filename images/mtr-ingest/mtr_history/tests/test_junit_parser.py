"""Tests for mtr_history.junit_parser against hand-crafted MTR XML fixtures.

Identities, statuses and merge rules mirror the real MTR output observed on
build #1558 of ps80/percona-server-8.0-pipeline-parallel-mtr.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from mtr_history.junit_parser import (
    RawTestRecord,
    _classify_file,
    _extract_suite_from_testsuite_name,
    _extract_worker,
    _parse_testcase_class,
    merge_test_records,
    parse_all_junit_files,
    parse_junit_file,
)


@pytest.mark.parametrize(
    "filename, expected",
    [
        ("junit_WORKER_1.xml",      ("regular", False)),
        ("junit_WORKER_2-big.xml",  ("regular", True)),
        ("junit_ci_fs.xml",         ("ci_fs", False)),
        ("junit_ps_protocol.xml",   ("ps_protocol", False)),
        ("junit_UNIT_TESTS.xml",    ("unit_tests", False)),
    ],
)
def test_classify_file(filename: str, expected) -> None:
    assert _classify_file(filename) == expected


@pytest.mark.parametrize(
    "name, expected",
    [
        ("oraclelinux-10.Debug.WORKER_1.rocksdb",             "WORKER_1"),
        ("oraclelinux-10.Debug.WORKER_2-big.innodb_undo",     "WORKER_2"),
        ("oraclelinux-10.Debug.WORKER_1.ci_fs.innodb",        "WORKER_1"),
        ("oraclelinux-10.Debug.WORKER_1.UNIT_TESTS.main",     "WORKER_1"),
        ("no-worker-here",                                    ""),
    ],
)
def test_extract_worker(name: str, expected: str) -> None:
    assert _extract_worker(name) == expected


@pytest.mark.parametrize(
    "klass, expected",
    [
        ("audit_log.audit_log_charset",         ("audit_log", "audit_log_charset")),
        ("engines/funcs.ai_init_alter_table",   ("engines/funcs", "ai_init_alter_table")),
        ("report.shutdown_report",              ("report", "shutdown_report")),
        ("no_dot_here",                         ("", "no_dot_here")),
    ],
)
def test_parse_class(klass: str, expected) -> None:
    assert _parse_testcase_class(klass) == expected


@pytest.mark.parametrize(
    "ts_name, expected",
    [
        # Regular worker: suite is the part after WORKER_N.
        ("oraclelinux-10.Debug.WORKER_2.audit_log",             "audit_log"),
        ("oraclelinux-10.Debug.WORKER_2.report",                "report"),
        ("oraclelinux-10.Debug.WORKER_1-big.innodb_undo",       "innodb_undo"),
        # UNIT_TESTS strips the context token.
        ("oraclelinux-10.Debug.WORKER_1.UNIT_TESTS.report",     "report"),
        ("oraclelinux-10.Debug.WORKER_1.UNIT_TESTS.main",       "main"),
        # ci_fs / ps_protocol likewise.
        ("oraclelinux-10.Debug.WORKER_1.ci_fs.innodb",          "innodb"),
        ("oraclelinux-10.Debug.WORKER_1.ps_protocol.main",      "main"),
        # Suite path with slash.
        ("oraclelinux-10.Debug.WORKER_4.engines/funcs",         "engines/funcs"),
        # Missing worker (shouldn't happen but we handle it).
        ("something.else",                                      ""),
    ],
)
def test_extract_suite_from_testsuite_name(ts_name: str, expected: str) -> None:
    assert _extract_suite_from_testsuite_name(ts_name) == expected


def _write(tmp: Path, name: str, body: str) -> Path:
    p = tmp / name
    p.write_text(body)
    return p


# --- full-file parsing -------------------------------------------------------

def test_parse_worker_file_pass_fail_skip(tmp_path: Path) -> None:
    xml = """<?xml version="1.0" encoding="UTF-8"?>
<testsuites>
  <testsuite name="oraclelinux-10.Debug.WORKER_2.audit_log" tests="3" failures="1" skip="1">
    <testcase class="audit_log.audit_log_charset" name="audit_log_charset" time="0">
      <failure message="Test failed" type="MTR_RES_FAILED"/>
      <system-out>stack trace here</system-out>
    </testcase>
    <testcase class="audit_log.audit_log_filter" name="audit_log_filter" time="8.32"/>
    <testcase class="audit_log.audit_log_big" name="audit_log_big" time="0">
      <skipped message="Test needs 'big-test' option" type="MTR_RES_SKIPPED"/>
    </testcase>
  </testsuite>
</testsuites>
"""
    f = _write(tmp_path, "junit_WORKER_2.xml", xml)
    records = parse_junit_file(f)
    assert len(records) == 3
    by_name = {r.name: r for r in records}
    assert by_name["audit_log_charset"].status == "fail"
    assert by_name["audit_log_charset"].worker == "WORKER_2"
    assert by_name["audit_log_charset"].big is False
    assert by_name["audit_log_charset"].run_context == "regular"
    assert by_name["audit_log_charset"].failure_message is not None
    assert "stack trace" in by_name["audit_log_charset"].failure_message

    assert by_name["audit_log_filter"].status == "pass"
    assert by_name["audit_log_filter"].time_s == pytest.approx(8.32)

    assert by_name["audit_log_big"].status == "skip"


def test_parse_big_file_marks_big_true(tmp_path: Path) -> None:
    xml = """<?xml version="1.0"?>
<testsuites>
  <testsuite name="oraclelinux-10.Debug.WORKER_1-big.innodb">
    <testcase class="innodb.big_row" name="big_row" time="123.45"/>
  </testsuite>
</testsuites>
"""
    f = _write(tmp_path, "junit_WORKER_1-big.xml", xml)
    records = parse_junit_file(f)
    assert len(records) == 1
    assert records[0].big is True
    assert records[0].worker == "WORKER_1"
    assert records[0].run_context == "regular"


def test_parse_ci_fs_file_sets_context(tmp_path: Path) -> None:
    xml = """<?xml version="1.0"?>
<testsuites>
  <testsuite name="oraclelinux-10.Debug.WORKER_1.ci_fs.innodb">
    <testcase class="innodb.innodb_bug60196" name="innodb_bug60196" time="13.4"/>
  </testsuite>
</testsuites>
"""
    f = _write(tmp_path, "junit_ci_fs.xml", xml)
    records = parse_junit_file(f)
    assert len(records) == 1
    assert records[0].run_context == "ci_fs"
    assert records[0].big is False


def test_parse_testcase_with_dotless_class_uses_testsuite_suite(tmp_path: Path) -> None:
    # Shape observed in real data on build 1558 for shutdown_report / unit_tests
    # failures: testcase class="shutdown_report" with no dot.
    xml = """<?xml version="1.0"?>
<testsuites>
  <testsuite name="oraclelinux-10.Debug.WORKER_2.report">
    <testcase name="shutdown_report" class="shutdown_report" time="0">
      <failure message="Test failed" type="MTR_RES_FAILED"/>
    </testcase>
  </testsuite>
</testsuites>
"""
    f = _write(tmp_path, "junit_WORKER_2.xml", xml)
    records = parse_junit_file(f)
    assert len(records) == 1
    assert records[0].suite == "report"
    assert records[0].name == "shutdown_report"
    assert records[0].status == "fail"
    assert records[0].worker == "WORKER_2"


def test_parse_testcase_variant_suffix_uses_name_attr(tmp_path: Path) -> None:
    # Real shape observed on build 1514 (ubuntu-noble):
    #   class="rpl_nogtid.rpl_gipk_cross_version_schema_changes"
    #   name="rpl_gipk_cross_version_schema_changes.row"
    # The `.row` variant lives in `name`; using `class` alone would collapse
    # row-variant and base-variant into one identity.
    xml = """<?xml version="1.0"?>
<testsuites>
  <testsuite name="ubuntu-noble.Debug.WORKER_7.rpl_nogtid">
    <testcase name="rpl_gipk_cross_version_schema_changes" class="rpl_nogtid.rpl_gipk_cross_version_schema_changes" time="5.0"/>
    <testcase name="rpl_gipk_cross_version_schema_changes.row" class="rpl_nogtid.rpl_gipk_cross_version_schema_changes" time="0">
      <failure message="Test failed" type="MTR_RES_FAILED"/>
    </testcase>
  </testsuite>
</testsuites>
"""
    f = _write(tmp_path, "junit_WORKER_7.xml", xml)
    records = parse_junit_file(f)
    # Must see BOTH the base test and the `.row` variant as distinct records.
    assert {(r.name, r.status) for r in records} == {
        ("rpl_gipk_cross_version_schema_changes", "pass"),
        ("rpl_gipk_cross_version_schema_changes.row", "fail"),
    }


def test_parse_unit_tests_context_class_no_dot(tmp_path: Path) -> None:
    # oraclelinux-10.Debug.WORKER_1.UNIT_TESTS.report / class="unit_tests"
    xml = """<?xml version="1.0"?>
<testsuites>
  <testsuite name="oraclelinux-10.Debug.WORKER_1.UNIT_TESTS.report">
    <testcase name="unit_tests" class="unit_tests" time="0">
      <failure message="Test failed" type="MTR_RES_FAILED"/>
    </testcase>
  </testsuite>
</testsuites>
"""
    f = _write(tmp_path, "junit_UNIT_TESTS.xml", xml)
    records = parse_junit_file(f)
    assert len(records) == 1
    assert records[0].run_context == "unit_tests"
    assert records[0].suite == "report"
    assert records[0].name == "unit_tests"
    assert records[0].status == "fail"


# --- merge rules -------------------------------------------------------------

def _rec(**kw) -> RawTestRecord:
    base = dict(
        suite="s", name="t", run_context="regular", worker="WORKER_1",
        big=False, status="pass", time_s=1.0, failure_message=None,
        source_file="junit_WORKER_1.xml",
    )
    base.update(kw)
    return RawTestRecord(**base)  # type: ignore[arg-type]


def test_merge_keeps_regular_and_big_as_distinct_records() -> None:
    # Sentinel case: report.shutdown_report can FAIL in both regular and -big
    # runs; they are separate executions and must both appear in the output.
    reg = _rec(status="fail", big=False, source_file="junit_WORKER_5.xml")
    big = _rec(status="fail", big=True, source_file="junit_WORKER_5-big.xml")
    out = merge_test_records([reg, big])
    assert len(out) == 2
    assert {r.big for r in out} == {False, True}


def test_merge_collapses_exact_identity_dupes() -> None:
    # Same XML emitting a duplicate (shouldn't happen in practice, but
    # guard against it): keep non-skip over skip.
    a = _rec(status="skip")
    b = _rec(status="pass")
    out = merge_test_records([a, b])
    assert len(out) == 1
    assert out[0].status == "pass"


def test_merge_keeps_different_run_contexts_separate() -> None:
    # Same (suite,name) but different run_context must NOT collapse.
    a = _rec(run_context="regular")
    b = _rec(run_context="ci_fs")
    out = merge_test_records([a, b])
    assert len(out) == 2
    assert {r.run_context for r in out} == {"regular", "ci_fs"}


def test_merge_keeps_different_workers_separate() -> None:
    # report.shutdown_report runs once per worker — WORKER_2 and WORKER_5
    # must remain separate records.
    a = _rec(worker="WORKER_2")
    b = _rec(worker="WORKER_5")
    out = merge_test_records([a, b])
    assert len(out) == 2
    assert {r.worker for r in out} == {"WORKER_2", "WORKER_5"}


# --- directory-level -------------------------------------------------------

def test_parse_all_junit_files_handles_missing_and_bad_xml(tmp_path: Path) -> None:
    # One good file.
    (tmp_path / "junit_WORKER_1.xml").write_text(
        '<testsuite name="oraclelinux-10.Debug.WORKER_1.main">'
        '<testcase class="main.t1" name="t1" time="0.1"/>'
        '</testsuite>'
    )
    # One broken file.
    (tmp_path / "junit_WORKER_2.xml").write_text("<not-xml")

    records, warnings = parse_all_junit_files(tmp_path)
    assert len(records) == 1
    assert records[0].name == "t1"
    assert any("parse error" in w for w in warnings)
