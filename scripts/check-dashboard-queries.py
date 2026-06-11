#!/usr/bin/env -S uv run --script
# /// script
# requires-python = ">=3.11"
# dependencies = []
# ///
"""check-dashboard-queries.py - validate every Grafana dashboard PromQL expr against Mimir.

Generic sibling of check-uptime-queries.py (which is wired to the jenkins-uptime
addon's recording rules and pre/post-deploy modes). This one takes any dashboard
JSON, walks every panel (including panels nested in collapsed rows), resolves the
dashboard's own template variables by executing their queries against Mimir, and
runs each target expr as an instant query. The point is a mechanical gate: a
dashboard change cannot claim its queries work, the script proves it.

Per expr the verdict is:
  OK                   returned 1+ series
  EMPTY (metric absent)  0 series and the metric name(s) do not exist in Mimir
                         at all - dead-metric evidence for panel removal
  EMPTY (selector)       0 series but the metric exists - label matchers (or
                         current inactivity) filtered everything out
  EMPTY-OK             0 series but allow-listed (panels legitimately empty
                       when healthy or idle: discards, 5xx overlays, bursty
                       flush quantiles)
  SKIP                 target uses a non-prometheus datasource (loki/tempo/...)
  ERROR                PromQL parse/eval error, or an unresolved $variable

Usage:
  scripts/check-dashboard-queries.py resources/addons/grafana/dashboards/foo.json
  scripts/check-dashboard-queries.py https://grafana.com/api/dashboards/17781/revisions/1/download
  scripts/check-dashboard-queries.py foo.json --var component='loki/.*' \
      --allow-empty-ref 27:B --allow-empty 'discard|panic'

Options:
  --prom-url URL       Mimir base (default: own port-forward to
                       svc/mimir-query-frontend :8080, context percona-ci-platform)
  --org-id ID          X-Scope-OrgID header (default percona-ci)
  --var NAME=VALUE     override a template variable (repeatable)
  --allow-empty RE     regex on panel title; matching EMPTY -> EMPTY-OK (repeatable)
  --allow-empty-ref R  exact "<panelId>:<refId>" allow-list entry (repeatable)
  --time T             instant-query evaluation time (unix or RFC3339; default now)
  --json               machine-readable output instead of the table

Read-only. Exit codes (scripts/README.md convention): 0 all OK/allowed,
1 any ERROR or disallowed EMPTY, 2 precondition failure (bad JSON, Mimir
unreachable). Requires kubectl on PATH unless --prom-url is given.
"""
from __future__ import annotations

import argparse
import atexit
import json
import re
import socket
import subprocess
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

CONTEXT = "percona-ci-platform"
PF_PORT = 58082

BUILTINS = {
    "__rate_interval": "5m",
    "__interval": "1m",
    "__interval_ms": "60000",
    "__range": "1h",
    "__range_s": "3600",
    "__range_ms": "3600000",
    "__auto": "5m",
}

# Aggregation operators and binary/grouping keywords that survive the
# strip passes but are not metric names.
PROMQL_KEYWORDS = {
    "sum", "min", "max", "avg", "group", "stddev", "stdvar", "count",
    "count_values", "bottomk", "topk", "quantile", "limitk", "limit_ratio",
    "by", "without", "on", "ignoring", "group_left", "group_right",
    "and", "or", "unless", "offset", "bool", "inf", "nan",
}

NON_PROM_DS = {"loki", "tempo", "postgres", "mysql", "elasticsearch", "cloudwatch"}


def fail(msg: str) -> None:
    print(f"precondition: {msg}", file=sys.stderr)
    sys.exit(2)


def port_forward() -> str:
    proc = subprocess.Popen(
        ["kubectl", "--context", CONTEXT, "-n", "mimir", "port-forward",
         "svc/mimir-query-frontend", f"{PF_PORT}:8080"],
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
    )
    atexit.register(proc.kill)
    for _ in range(40):
        try:
            with socket.create_connection(("127.0.0.1", PF_PORT), timeout=0.5):
                return f"http://127.0.0.1:{PF_PORT}"
        except OSError:
            if proc.poll() is not None:
                fail("port-forward exited; check kubeconfig/context and AWS creds")
            time.sleep(0.5)
    fail("port-forward never became reachable")
    raise AssertionError  # unreachable


class Mimir:
    def __init__(self, base: str, org_id: str):
        self.base = base
        self.org_id = org_id
        self._names: set[str] | None = None

    def api(self, path: str, **params) -> dict:
        qs = urllib.parse.urlencode(params)
        req = urllib.request.Request(
            f"{self.base}/prometheus{path}?{qs}",
            headers={"X-Scope-OrgID": self.org_id},
        )
        try:
            with urllib.request.urlopen(req, timeout=30) as r:
                return json.load(r)
        except urllib.error.HTTPError as e:
            try:
                return json.load(e)
            except Exception:
                return {"status": "error", "error": f"HTTP {e.code}"}
        except (urllib.error.URLError, TimeoutError) as e:
            fail(f"Mimir unreachable at {self.base}: {e}")
            raise AssertionError  # unreachable

    def label_values(self, label: str, match: str | None = None) -> list[str]:
        params = {"match[]": match} if match else {}
        res = self.api(f"/api/v1/label/{label}/values", **params)
        return res.get("data", []) if res.get("status") == "success" else []

    def metric_names(self) -> set[str]:
        if self._names is None:
            self._names = set(self.label_values("__name__"))
        return self._names


# --- template variable resolution -------------------------------------------

LABEL_VALUES_RE = re.compile(r"label_values\((?:(.+),\s*)?(\w+)\)\s*$", re.DOTALL)


def var_query_string(var: dict) -> str:
    q = var.get("query", "")
    if isinstance(q, dict):
        q = q.get("query", "")
    return q or ""


def resolve_variables(dash: dict, mimir: Mimir, overrides: dict[str, str]) -> dict[str, list[str]]:
    """name -> list of values (len>1 only for multi/includeAll vars)."""
    resolved: dict[str, list[str]] = {}
    for var in dash.get("templating", {}).get("list", []):
        name, vtype = var.get("name", ""), var.get("type", "")
        if not name:
            continue
        if name in overrides:
            resolved[name] = overrides[name].split("|")
            continue
        if vtype == "datasource":
            continue  # lives in datasource fields, not exprs
        if vtype == "query":
            q = var_query_string(var)
            multi = var.get("multi") or var.get("includeAll")
            if multi and var.get("includeAll") and var.get("allValue"):
                resolved[name] = [var["allValue"]]
                continue
            m = LABEL_VALUES_RE.fullmatch(q.strip())
            if m:
                values = mimir.label_values(m.group(2), m.group(1))
            elif q.strip().startswith("query_result("):
                res = mimir.api("/api/v1/query", query=q.strip()[len("query_result("):-1])
                values = [r["metric"].get("__name__", "") + json.dumps(r["metric"])
                          for r in res.get("data", {}).get("result", [])]
            else:  # unsupported query form; surfaces as unresolved downstream
                values = []
            if rx := var.get("regex"):
                rx = rx.strip("/")
                hits = []
                for v in values:
                    if m2 := re.search(rx, v):
                        hits.append(m2.group(1) if m2.groups() else m2.group(0))
                values = hits
            if not values:
                resolved[name] = []
            elif multi:
                resolved[name] = sorted(set(values))
            else:
                resolved[name] = [sorted(set(values))[0]]
        elif vtype in ("custom", "interval", "constant", "textbox"):
            cur = var.get("current", {}).get("value", "")
            if isinstance(cur, list):
                cur = cur[0] if cur else ""
            if not cur or cur in ("$__auto_interval", "$__auto"):
                opts = [o.get("value", "") for o in var.get("options", [])]
                cur = opts[0] if opts else var_query_string(var).split(",")[0]
            resolved[name] = [cur]
    return resolved


FMT_RE = re.compile(r"\$\{(\w+)(?::(\w+))?\}|\$(\w+)")


def substitute(expr: str, variables: dict[str, list[str]]) -> tuple[str, list[str]]:
    """Replace vars; return (expr, unresolved-names)."""
    unresolved: list[str] = []

    def repl(m: re.Match) -> str:
        name = m.group(1) or m.group(3)
        fmt = m.group(2)
        if name in BUILTINS:
            return BUILTINS[name]
        if name not in variables:
            unresolved.append(f"${name}")
            return m.group(0)
        vals = variables[name]
        if not vals:
            unresolved.append(f"${name} (resolved to no values)")
            return m.group(0)
        if fmt == "csv":
            return ",".join(vals)
        if fmt == "pipe":
            return "|".join(vals)
        if len(vals) == 1:
            return vals[0]
        # multi-value default / :regex - regex alternation. PromQL string
        # literals use Go escaping, so the regex backslash itself must be
        # doubled (master=~"ps3\.cd" is a parse error; Grafana emits ps3\\.cd).
        return "(" + "|".join(re.escape(v).replace("\\", "\\\\") for v in vals) + ")"

    return FMT_RE.sub(repl, expr), unresolved


# --- static metric-name extraction -------------------------------------------

def extract_metric_names(expr: str) -> list[str]:
    s = re.sub(r'"(?:[^"\\]|\\.)*"|\'(?:[^\'\\]|\\.)*\'', "", expr)
    s = re.sub(r"\{[^}]*\}", "", s)
    s = re.sub(r"\[[^\]]*\]", "", s)
    s = re.sub(r"\b(by|without|on|ignoring|group_left|group_right)\s*\([^)]*\)", " ", s)
    names = []
    for m in re.finditer(r"[a-zA-Z_:][a-zA-Z0-9_:]*", s):
        tok = m.group(0)
        if tok in PROMQL_KEYWORDS:
            continue
        if s[m.end():m.end() + 1] == "(" or s[m.end():].lstrip()[:1] == "(":
            continue  # function call
        names.append(tok)
    return sorted(set(names))


# --- panel walking ------------------------------------------------------------

def walk_panels(dash: dict):
    def inner(panels):
        for p in panels:
            if p.get("type") == "row":
                yield from inner(p.get("panels", []))
            else:
                yield p
    yield from inner(dash.get("panels", []))
    for row in dash.get("rows", []):  # legacy pre-schema-16 shape
        yield from inner(row.get("panels", []))


def panel_ds_type(panel: dict, target: dict) -> str:
    for obj in (target, panel):
        ds = obj.get("datasource")
        if isinstance(ds, dict) and ds.get("type"):
            return ds["type"]
        if isinstance(ds, str) and ds and not ds.startswith("$"):
            return ds.lower()
    return "prometheus"


def load_dashboard(src: str) -> dict:
    if src.startswith(("http://", "https://")):
        req = urllib.request.Request(src, headers={"User-Agent": "percona-cd-platform-check-dashboard-queries"})
        try:
            with urllib.request.urlopen(req, timeout=30) as r:
                raw = r.read().decode()
        except (urllib.error.URLError, TimeoutError) as e:
            fail(f"cannot fetch {src}: {e}")
    else:
        path = Path(src)
        if not path.exists():
            fail(f"no such file: {src}")
        raw = path.read_text()
    try:
        d = json.loads(raw)
    except json.JSONDecodeError as e:
        fail(f"invalid JSON in {src}: {e}")
        raise AssertionError  # unreachable
    return d.get("dashboard", d)  # /api/dashboards/uid wrapping


# --- main ---------------------------------------------------------------------

def check_dashboard(src: str, mimir: Mimir, args) -> tuple[list[dict], int]:
    dash = load_dashboard(src)
    overrides = dict(v.split("=", 1) for v in args.var)
    variables = resolve_variables(dash, mimir, overrides)
    allow_res = [re.compile(p, re.IGNORECASE) for p in args.allow_empty]
    allow_refs = set(args.allow_empty_ref)

    rows, failures = [], 0
    for panel in walk_panels(dash):
        for target in panel.get("targets", []):
            expr = target.get("expr", "")
            if not expr:
                continue
            pid, ref = panel.get("id", "?"), target.get("refId", "?")
            title = panel.get("title", "")[:38]
            if target.get("hide"):
                ref = f"{ref}*"
            record = {"panel": pid, "title": title, "ref": ref, "expr": expr,
                      "series": 0, "absent": [], "note": ""}

            ds = panel_ds_type(panel, target)
            if ds in NON_PROM_DS:
                record["status"] = "SKIP"
                record["note"] = f"{ds} datasource"
                rows.append(record)
                continue

            sub, unresolved = substitute(expr, variables)
            if unresolved:
                record["status"] = "ERROR"
                record["note"] = "unresolved " + ", ".join(sorted(set(unresolved)))
                failures += 1
                rows.append(record)
                continue

            params = {"query": sub}
            if args.time:
                params["time"] = args.time
            res = mimir.api("/api/v1/query", **params)
            if res.get("status") != "success":
                record["status"] = "ERROR"
                record["note"] = str(res.get("error", ""))[:100]
                failures += 1
            else:
                n = len(res.get("data", {}).get("result", []))
                record["series"] = n
                if n > 0:
                    record["status"] = "OK"
                else:
                    absent = [m for m in extract_metric_names(sub)
                              if m not in mimir.metric_names()]
                    record["absent"] = absent
                    allowed = (f"{pid}:{target.get('refId', '?')}" in allow_refs
                               or any(r.search(panel.get("title", "")) for r in allow_res))
                    if allowed:
                        record["status"] = "EMPTY-OK"
                    elif absent:
                        record["status"] = "EMPTY (metric absent)"
                        failures += 1
                    else:
                        record["status"] = "EMPTY (selector)"
                        failures += 1
            rows.append(record)
    return rows, failures


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("dashboards", nargs="+", metavar="DASHBOARD",
                    help="dashboard JSON path or URL")
    ap.add_argument("--prom-url", help="Mimir base URL (skips port-forward)")
    ap.add_argument("--org-id", default="percona-ci")
    ap.add_argument("--var", action="append", default=[], metavar="NAME=VALUE")
    ap.add_argument("--allow-empty", action="append", default=[], metavar="REGEX")
    ap.add_argument("--allow-empty-ref", action="append", default=[], metavar="ID:REF")
    ap.add_argument("--time", help="instant-query time (unix or RFC3339)")
    ap.add_argument("--json", action="store_true")
    args = ap.parse_args()
    for v in args.var:
        if "=" not in v:
            fail(f"--var wants NAME=VALUE, got {v!r}")

    mimir = Mimir(args.prom_url or port_forward(), args.org_id)

    total_failures = 0
    all_rows = []
    for src in args.dashboards:
        rows, failures = check_dashboard(src, mimir, args)
        total_failures += failures
        all_rows.append({"dashboard": src, "results": rows})
        if args.json:
            continue
        wt = max((len(r["title"]) for r in rows), default=5) + 2
        ws = max((len(r["status"]) for r in rows), default=6) + 2
        print(f"== {src}")
        print(f"{'PANEL':<7}{'TITLE':<{wt}}{'REF':<5}{'STATUS':<{ws}}{'SERIES':<8}{'ABSENT-METRICS':<34}EXPR")
        for r in rows:
            absent = ",".join(r["absent"])[:32] or "-"
            expr = re.sub(r"\s+", " ", r["expr"])[:60]
            note = f"  # {r['note']}" if r["note"] else ""
            print(f"{r['panel']:<7}{r['title']:<{wt}}{r['ref']:<5}{r['status']:<{ws}}"
                  f"{r['series']:<8}{absent:<34}{expr}{note}")
        counts: dict[str, int] = {}
        for r in rows:
            key = r["status"].split(" ")[0]
            counts[key] = counts.get(key, 0) + 1
        summary = "  ".join(f"{k}={v}" for k, v in sorted(counts.items()))
        print(f"-- {len(rows)} exprs: {summary}\n")

    if args.json:
        print(json.dumps(all_rows, indent=2))
    return 1 if total_failures else 0


if __name__ == "__main__":
    sys.exit(main())
