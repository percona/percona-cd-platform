"""JUnit XML → RawTestRecord, with MTR-specific identity and merge rules.

The MTR pipeline writes 19 junit XMLs per full build:
  junit_WORKER_{1..8}.xml        — regular per-worker run
  junit_WORKER_{1..8}-big.xml    — big-tests per-worker run
  junit_ci_fs.xml                — ci-filesystem suite (always WORKER_1)
  junit_ps_protocol.xml          — prepared-statement-protocol suite (always WORKER_1)
  junit_UNIT_TESTS.xml           — unit tests (always WORKER_1)

Test identity is (suite, name, run_context).  `worker` and `big` are *attributes*.

Tested against Percona Server 8.0 MTR builds; see plan, Phase 1 findings.
"""

from __future__ import annotations

import re
import xml.etree.ElementTree as ET
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

RunContext = Literal["regular", "ci_fs", "ps_protocol", "unit_tests"]

# Max size of <system-out> or <failure> text captured into failure_message, bytes.
_MAX_FAILURE_MESSAGE_BYTES = 2048

# Matches WORKER_<N>[-big] in a testsuite.name string.
_WORKER_RE = re.compile(r"WORKER_(\d+)(-big)?")


@dataclass
class RawTestRecord:
    suite: str
    name: str
    run_context: RunContext
    worker: str          # "WORKER_2" (no -big suffix, even for big-XML rows)
    big: bool
    status: Literal["pass", "fail", "skip"]
    time_s: float
    failure_message: str | None
    source_file: str     # junit_WORKER_2-big.xml — for debugging / traceability


def _classify_file(filename: str) -> tuple[RunContext, bool]:
    """Return (run_context, big) based on the XML filename."""
    if filename == "junit_ci_fs.xml":
        return "ci_fs", False
    if filename == "junit_ps_protocol.xml":
        return "ps_protocol", False
    if filename == "junit_UNIT_TESTS.xml":
        return "unit_tests", False
    if filename.endswith("-big.xml"):
        return "regular", True
    return "regular", False


def _extract_worker(testsuite_name: str) -> str:
    """Return "WORKER_<N>" from a testsuite name; empty string if none present.

    testsuite.name is shaped like "{os}.{build_type}.WORKER_{N}[-big][.ctx].{suite}".
    Special XMLs (ci_fs/ps_protocol/UNIT_TESTS) still carry WORKER_1 in their name.
    """
    m = _WORKER_RE.search(testsuite_name)
    if not m:
        return ""
    return f"WORKER_{m.group(1)}"


# Context tokens that appear as one of the parts after WORKER_N in the
# testsuite.name for the special XMLs.  These are skipped when extracting suite.
_CONTEXT_TOKENS = {"ci_fs", "ps_protocol", "UNIT_TESTS"}


def _extract_suite_from_testsuite_name(testsuite_name: str) -> str:
    """Return the suite segment of a `<testsuite name>` attribute.

    Format is "{os}.{build_type}.WORKER_{N}[-big][.{ctx}].{suite}".

    Examples:
      oraclelinux-10.Debug.WORKER_2.audit_log               → audit_log
      oraclelinux-10.Debug.WORKER_2.report                  → report
      oraclelinux-10.Debug.WORKER_1.UNIT_TESTS.report       → report
      oraclelinux-10.Debug.WORKER_1.ci_fs.innodb            → innodb
      oraclelinux-10.Debug.WORKER_1-big.innodb_undo         → innodb_undo
      oraclelinux-10.Debug.WORKER_1.engines/funcs           → engines/funcs
    """
    parts = testsuite_name.split(".")
    # Find the WORKER_N[-big] part; everything after it (minus a possible
    # context token) is the suite path.
    for i, part in enumerate(parts):
        if _WORKER_RE.fullmatch(part):
            tail = parts[i + 1:]
            if tail and tail[0] in _CONTEXT_TOKENS:
                tail = tail[1:]
            return ".".join(tail)
    return ""


def _parse_testcase_class(class_attr: str) -> tuple[str, str]:
    """Split <testcase class="suite.name"> on FIRST dot after the suite.

    MTR suites may contain slashes (e.g. "engines/funcs.ai_init_alter_table").
    We split on the last dot to keep suite paths with slashes intact.
    Actually: MTR's convention is suite."name", so we split on the LAST dot.

    Tested against real data from build 1558:
        "audit_log.audit_log_charset"       → ("audit_log", "audit_log_charset")
        "engines/funcs.ai_init_alter_table" → ("engines/funcs", "ai_init_alter_table")
        "report.shutdown_report"            → ("report", "shutdown_report")
    """
    if "." not in class_attr:
        return ("", class_attr)
    # Split on the LAST dot: suites with "/" keep their structure intact,
    # and the last segment is always the test name.
    idx = class_attr.rfind(".")
    return class_attr[:idx], class_attr[idx + 1:]


def _status_and_message(tc: ET.Element) -> tuple[str, str | None]:
    """Return (status, failure_message-or-None) from a <testcase>.

    JUnit produced by MTR (via mysql-test-run.pl --junit-output) uses:
      <failure message="Test failed" type="MTR_RES_FAILED"/>    → fail
      <skipped message="..." type="MTR_RES_SKIPPED"/>           → skip
      (no child)                                                → pass
    Optional sibling <system-out> captures stdout for failed/skipped cases.
    """
    failure = tc.find("failure")
    if failure is not None:
        msg = failure.get("message", "")
        sysout = tc.find("system-out")
        sysout_text = (sysout.text or "") if sysout is not None else ""
        body = (msg + ("\n" + sysout_text if sysout_text else "")).strip()
        if len(body.encode("utf-8")) > _MAX_FAILURE_MESSAGE_BYTES:
            body = body.encode("utf-8")[:_MAX_FAILURE_MESSAGE_BYTES].decode("utf-8", errors="ignore")
        return "fail", body or "Test failed"
    skipped = tc.find("skipped")
    if skipped is not None:
        return "skip", None
    return "pass", None


def parse_junit_file(xml_path: Path) -> list[RawTestRecord]:
    """Parse a single junit_*.xml and return RawTestRecords."""
    run_context, big = _classify_file(xml_path.name)
    tree = ET.parse(xml_path)
    root = tree.getroot()
    # MTR's XML may have a <testsuites> root wrapping multiple <testsuite>
    # or a single <testsuite> as root.  Handle both.
    suites: list[ET.Element]
    if root.tag == "testsuites":
        suites = list(root.findall("testsuite"))
    elif root.tag == "testsuite":
        suites = [root]
    else:
        return []

    records: list[RawTestRecord] = []
    for ts in suites:
        suite_name_attr = ts.get("name", "")
        worker = _extract_worker(suite_name_attr)
        suite_from_testsuite = _extract_suite_from_testsuite_name(suite_name_attr)

        for tc in ts.findall("testcase"):
            class_attr = tc.get("class") or tc.get("classname") or ""
            name_attr = tc.get("name") or ""

            # MTR encodes the test name in the `name` attribute (including any
            # variant suffix like `.row`, `.ps_protocol`, `.ps`).  The `class`
            # attribute is either "suite.base_name" or just "base_name" — it
            # does NOT carry the variant.  Always prefer `name` for the test
            # identity.
            if "." in class_attr:
                suite, _class_name = _parse_testcase_class(class_attr)
            else:
                suite = suite_from_testsuite
            name = name_attr or class_attr
            if not suite or not name:
                continue
            time_str = tc.get("time", "0")
            try:
                time_s = float(time_str)
            except (TypeError, ValueError):
                time_s = 0.0
            status, failure_message = _status_and_message(tc)

            records.append(
                RawTestRecord(
                    suite=suite,
                    name=name,
                    run_context=run_context,
                    worker=worker,
                    big=big,
                    status=status,  # type: ignore[arg-type]
                    time_s=time_s,
                    failure_message=failure_message,
                    source_file=xml_path.name,
                )
            )
    return records


def merge_test_records(records: list[RawTestRecord]) -> list[RawTestRecord]:
    """Deduplicate by full identity (suite, name, run_context, worker, big).

    MTR emits two worker XMLs (regular and -big) that share many test names,
    but crucially each side is an independent execution: a sentinel test such
    as `report.shutdown_report` can PASS in the regular run and FAIL in the
    -big run (or vice versa).  Merging across `big` would hide such failures
    — verified empirically on ubuntu-noble build 1514 (Jenkins reports 21
    failures, merge-across-big parser finds only 19).

    This deduplicator only collapses exact-identity repeats within the same
    XML file (which MTR does not emit in practice); it never merges across
    regular/-big.  The 2,201 "Not a big test" placeholder-skips in the -big
    XMLs therefore stay as skip records — downstream consumers filter them
    via `status=skip AND failure_message matches 'Not a big test'` if desired.
    """
    keyed: dict[tuple[str, str, str, str, bool], RawTestRecord] = {}
    for r in records:
        key = (r.suite, r.name, r.run_context, r.worker, r.big)
        # On exact-identity dupes (shouldn't happen), keep the non-skip side.
        existing = keyed.get(key)
        if existing is None or (existing.status == "skip" and r.status != "skip"):
            keyed[key] = r
    return list(keyed.values())


def parse_all_junit_files(xml_dir: Path) -> tuple[list[RawTestRecord], list[str]]:
    """Parse every junit_*.xml under `xml_dir` and apply the merge rule.

    Returns (merged records, warnings[]).  Parse errors on individual files
    become warnings; other files still parse.
    """
    records: list[RawTestRecord] = []
    warnings: list[str] = []
    for xml_path in sorted(xml_dir.rglob("junit_*.xml")):
        try:
            records.extend(parse_junit_file(xml_path))
        except ET.ParseError as e:
            warnings.append(f"parse error in {xml_path.name}: {e}")
        except OSError as e:
            warnings.append(f"read error in {xml_path.name}: {e}")
    merged = merge_test_records(records)
    return merged, warnings
