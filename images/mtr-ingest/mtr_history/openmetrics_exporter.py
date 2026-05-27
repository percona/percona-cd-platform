"""Per-build JSON → OpenMetrics text → merged file ready for `promtool`.

See plan Stage B.  Four metrics:
  mtr_test_status            — value 1/0/2 per test per build
  mtr_test_duration_seconds  — value seconds per test per build
  mtr_build_summary_total    — pass/fail/skip counts per build (3 samples)
  mtr_build_info             — one 1-valued sample per build with rich labels
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterable, Iterator

# --- label value sanitization ------------------------------------------------

_LABEL_ESCAPE_RE = re.compile(r'([\\"])')


def _escape(v: str) -> str:
    return _LABEL_ESCAPE_RE.sub(r"\\\1", v.replace("\n", " ")).strip()


def _fmt_labels(labels: dict[str, str]) -> str:
    # OpenMetrics canonical form: sorted by label name, quoted values.
    parts = []
    for k in sorted(labels.keys()):
        parts.append(f'{k}="{_escape(str(labels[k]))}"')
    return "{" + ",".join(parts) + "}"


# --- status enum -------------------------------------------------------------

_STATUS_VALUE: dict[str, float] = {"pass": 1.0, "fail": 0.0, "skip": 2.0}


# --- platform_os normalization for label compat -----------------------------

def _normalize_os(docker_os: str) -> str:
    # "oraclelinux:10" → "oraclelinux-10" — Prometheus labels prefer no colons.
    return docker_os.replace(":", "-")


# --- extract test-identity helpers ------------------------------------------

def _worker_slug(worker: str, big: bool, run_context: str) -> str:
    """Worker label as used in the Jenkins testReport URL path."""
    if run_context == "regular":
        return f"{worker}-big" if big else worker
    # Special contexts: path component is e.g. "WORKER_1.ci_fs" in Jenkins URLs.
    # For testReport URL convenience we emit the context slug instead.
    return {"ci_fs": "ci_fs", "ps_protocol": "ps_protocol", "unit_tests": "UNIT_TESTS"}[run_context]


def _worker_num(worker: str, run_context: str) -> str:
    if run_context != "regular":
        return ""
    # "WORKER_2" → "2".
    if worker.startswith("WORKER_"):
        return worker.removeprefix("WORKER_")
    return ""


def _instance_from_url(url: str) -> str:
    # "https://ps80.cd.percona.com/job/..." → "ps80.cd.percona.com"
    try:
        return url.split("://", 1)[1].split("/", 1)[0]
    except IndexError:
        return "unknown"


def _job_name_from_url(url: str) -> str:
    # Extract the first segment after /job/.
    try:
        tail = url.split("/job/", 1)[1]
        return tail.split("/", 1)[0]
    except IndexError:
        return "unknown"


def _build_label(build_number: int) -> str:
    return f"{build_number:05d}"


def _ts_seconds(iso: str) -> float:
    # Parse ISO 8601 with Z suffix, return Unix seconds with ms precision.
    if iso.endswith("Z"):
        iso = iso[:-1] + "+00:00"
    dt = datetime.fromisoformat(iso)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.timestamp()


# --- sample type ------------------------------------------------------------

@dataclass(slots=True)
class Sample:
    metric: str
    labels: dict[str, str]
    value: float
    ts_seconds: float

    def render(self) -> str:
        return f"{self.metric}{_fmt_labels(self.labels)} {self.value:.6g} {self.ts_seconds:.3f}"


# --- per-build sample emission ----------------------------------------------

def iter_samples(build_json: dict) -> Iterator[Sample]:
    """Yield all samples for one build JSON document."""
    url = build_json["url"]
    instance = _instance_from_url(url)
    job_name = _job_name_from_url(url)
    build = _build_label(build_json["build_number"])
    ts = _ts_seconds(build_json["timestamp"])

    plat = build_json["platform"]
    src = build_json["source"]
    platform_os = _normalize_os(plat["docker_os"])
    platform_build = plat["cmake_build_type"]
    arch = plat["arch"]
    branch = src.get("branch") or ""
    fork = src.get("fork") or ""
    # Unpadded build number for Jenkins URL templates (Jenkins uses /1558/, not /01558/).
    build_n = str(build_json["build_number"])

    # --- build_info -----
    cause = build_json.get("cause") or {}
    build_info_labels = {
        "instance": instance,
        "job_name": job_name,
        "build": build,
        "build_n": build_n,
        "platform_os": platform_os,
        "platform_build": platform_build,
        "arch": arch,
        "branch": branch,
        "fork": fork,
        "result": build_json.get("result", "UNKNOWN"),
        "cause_kind": cause.get("kind", "Unknown"),
        "cause_user": cause.get("user") or "",
        "build_url": url,
    }
    yield Sample("mtr_build_info", build_info_labels, 1.0, ts)

    # --- build_summary_total -----
    summary = build_json["summary"]
    for status_key, json_key in (("pass", "pass"), ("fail", "fail"), ("skip", "skip")):
        labels = {
            "instance": instance,
            "job_name": job_name,
            "build": build,
            "build_n": build_n,
            "platform_os": platform_os,
            "platform_build": platform_build,
            "arch": arch,
            "branch": branch,
            "fork": fork,
            "status": status_key,
        }
        yield Sample("mtr_build_summary_total", labels, float(summary.get(json_key, 0)), ts)

    # --- per-test status + duration -----
    for t in build_json.get("tests", []):
        base = {
            "instance": instance,
            "job_name": job_name,
            "build": build,
            "build_n": build_n,
            "suite": t["suite"],
            "testname": t["name"],
            "platform_os": platform_os,
            "platform_build": platform_build,
            "arch": arch,
            "branch": branch,
            "fork": fork,
            "run_context": t["run_context"],
            "worker_slug": _worker_slug(t["worker"], t["big"], t["run_context"]),
            "worker_num": _worker_num(t["worker"], t["run_context"]),
        }
        yield Sample("mtr_test_status", base, _STATUS_VALUE[t["status"]], ts)
        yield Sample("mtr_test_duration_seconds", base, float(t.get("time_s", 0.0)), ts)

        # Failure info: only for failed tests, carries a truncated failure_msg label.
        if t["status"] == "fail":
            msg = (t.get("failure_message") or "")[:200].replace("\n", " ").strip()
            yield Sample("mtr_test_failure_info", {**base, "failure_msg": msg}, 1.0, ts)


# --- helpers for file output ------------------------------------------------

_METRIC_HELP: dict[str, tuple[str, str]] = {
    # (HELP text, TYPE)
    "mtr_build_info":            ("Jenkins MTR build metadata (labels only)", "gauge"),
    "mtr_build_summary_total":   ("MTR build test counts by status", "gauge"),
    "mtr_test_failure_info":     ("Failed test with truncated failure message", "gauge"),
    "mtr_test_status":           ("Per-test result (1=PASS, 0=FAIL, 2=SKIP)", "gauge"),
    "mtr_test_duration_seconds": ("Per-test execution time in seconds", "gauge"),
}


def _canonical_sort_key(line: str) -> str:
    # Lines begin with `metric{...} value ts`; the metric name + labels already
    # form a canonical prefix (labels are sorted inside _fmt_labels).  A plain
    # lexicographic sort yields (metric, labels, ts) ordering.
    return line


def _metric_name(line: str) -> str:
    # Line shape: 'metric_name{labels} value ts' → metric_name
    brace = line.find("{")
    space = line.find(" ")
    end = min(x for x in (brace, space) if x != -1) if brace != -1 or space != -1 else len(line)
    return line[:end]


def write_openmetrics(samples: Iterable[Sample], out_path: Path) -> int:
    """Write sorted OpenMetrics text for one build's samples.  Returns count.

    OpenMetrics requires HELP/TYPE lines to immediately precede the samples of
    their metric family; we emit metric families in sorted order with their
    headers inline, and terminate with a single `# EOF`.
    """
    rendered = [s.render() for s in samples]
    rendered.sort(key=_canonical_sort_key)

    out_path.parent.mkdir(parents=True, exist_ok=True)
    total = len(rendered)
    with out_path.open("w") as f:
        current_metric: str | None = None
        for line in rendered:
            metric = _metric_name(line)
            if metric != current_metric:
                help_text, typ = _METRIC_HELP.get(metric, (metric, "gauge"))
                f.write(f"# HELP {metric} {help_text}\n")
                f.write(f"# TYPE {metric} {typ}\n")
                current_metric = metric
            f.write(line + "\n")
        f.write("# EOF\n")
    return total


def build_openmetrics_bundle(json_dir: Path, out_dir: Path) -> dict:
    """For every per-build JSON in `json_dir`, emit one .openmetrics.txt in `out_dir`."""
    out_dir.mkdir(parents=True, exist_ok=True)
    total = 0
    files: list[str] = []
    for json_path in sorted(json_dir.glob("*.json")):
        data = json.loads(json_path.read_text())
        # Skip tombstones — no test samples to emit, but we still want build_info.
        build_number = data.get("build_number")
        if build_number is None:
            continue
        out_path = out_dir / f"{_build_label(build_number)}.openmetrics.txt"
        count = write_openmetrics(iter_samples(data), out_path)
        total += count
        files.append(out_path.name)
    return {"builds_processed": len(files), "total_samples": total, "files": files}


def merge_openmetrics_files(in_dir: Path, out_path: Path) -> int:
    """Concatenate + globally sort per-build files into one promtool input file.

    HELP/TYPE lines are emitted once per metric family at the family's first
    appearance.  promtool's create-blocks-from-openmetrics rejects repeated
    HELP/TYPE blocks for the same metric, so the merged file must be a single
    pass over all samples in canonical (metric, labels, timestamp) order.
    """
    sample_lines: list[str] = []
    for p in sorted(in_dir.glob("*.openmetrics.txt")):
        with p.open() as f:
            for line in f:
                line = line.rstrip("\n")
                if not line or line.startswith("#"):
                    continue
                sample_lines.append(line)
    sample_lines.sort(key=_canonical_sort_key)

    out_path.parent.mkdir(parents=True, exist_ok=True)
    with out_path.open("w") as f:
        current_metric: str | None = None
        for line in sample_lines:
            metric = _metric_name(line)
            if metric != current_metric:
                help_text, typ = _METRIC_HELP.get(metric, (metric, "gauge"))
                f.write(f"# HELP {metric} {help_text}\n")
                f.write(f"# TYPE {metric} {typ}\n")
                current_metric = metric
            f.write(line + "\n")
        f.write("# EOF\n")
    return len(sample_lines)
