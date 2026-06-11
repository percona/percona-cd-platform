"""Validate every jenkins-uptime PromQL query against Mimir.

Collects all queries the jenkins-uptime addon ships (ADR 0031): the
recording-rule exprs from the PrometheusRule (live from the cluster when
deployed, else rendered via `helm template`) and every panel expr plus the
template-variable query from the Jenkins Uptime dashboard JSON. Runs each
against the Mimir query-frontend and reports parse validity and series counts
in one table (kubectl-style; --json / --llm for machine output).

Two modes, auto-detected from whether probe_success{job=~"jenkins-uptime-.*"}
has series:
  pre-deploy   probe_*/jenkins:* queries must PARSE but are expected empty;
               source queries (hetzner_*) must return series.
  post-deploy  every query must parse AND return series.

Read-only. Exits nonzero on parse errors or unexpectedly empty results.
Requires kubectl on PATH (unless --url) and helm (unless the PrometheusRule
is already live in the cluster). Kubeconfig context: percona-ci-platform.
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

import yaml

from cdpctl import _stage
from cdpctl._repo import die, repo_root

CONTEXT = "percona-ci-platform"
TENANT = "percona-ci"
PF_PORT = 58081

# Series the addon itself creates: absent until it is deployed.
ADDON_SERIES = re.compile(r"probe_[a-z_]+|jenkins:[a-z_:]+")


def port_forward() -> str:
    proc = subprocess.Popen(
        [
            "kubectl",
            "--context",
            CONTEXT,
            "-n",
            "mimir",
            "port-forward",
            "svc/mimir-query-frontend",
            f"{PF_PORT}:8080",
        ],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    atexit.register(proc.kill)
    for _ in range(40):
        try:
            with socket.create_connection(("127.0.0.1", PF_PORT), timeout=0.5):
                return f"http://127.0.0.1:{PF_PORT}"
        except OSError:
            if proc.poll() is not None:
                die("port-forward exited; check kubeconfig/context and AWS creds")
            time.sleep(0.5)
    die("port-forward never became reachable")
    raise AssertionError  # unreachable


def api(base: str, path: str, **params) -> dict:
    qs = urllib.parse.urlencode(params)
    req = urllib.request.Request(f"{base}/prometheus{path}?{qs}", headers={"X-Scope-OrgID": TENANT})
    try:
        with urllib.request.urlopen(req, timeout=30) as r:
            return json.load(r)
    except urllib.error.HTTPError as e:
        try:
            return json.load(e)
        except Exception:
            return {"status": "error", "error": f"HTTP {e.code}"}


def rule_exprs() -> tuple[str, list[tuple[str, str]]]:
    """(source, [(rule_name, expr)]) from the live cluster, else helm template."""
    chart = f"{repo_root()}/resources/addons/jenkins-uptime"
    live = subprocess.run(
        [
            "kubectl",
            "--context",
            CONTEXT,
            "-n",
            "jenkins-uptime",
            "get",
            "prometheusrule",
            "jenkins-uptime",
            "-o",
            "yaml",
        ],
        capture_output=True,
        text=True,
    )
    if live.returncode == 0:
        src, text = "cluster", live.stdout
    else:
        rendered = subprocess.run(
            [
                "helm",
                "template",
                "jenkins-uptime",
                chart,
                "--namespace",
                "jenkins-uptime",
                "--show-only",
                "templates/prometheusrule-jenkins-uptime.yaml",
            ],
            capture_output=True,
            text=True,
        )
        if rendered.returncode != 0:
            die(
                f"helm template failed (run `helm dependency build {chart}`?):\n"
                f"{rendered.stderr.strip()}"
            )
        src, text = "helm", rendered.stdout
    doc = next(d for d in yaml.safe_load_all(text) if d and d.get("kind") == "PrometheusRule")
    return src, [
        (r["record"], r["expr"].strip()) for g in doc["spec"]["groups"] for r in g["rules"]
    ]


def dashboard_queries() -> list[tuple[str, str]]:
    path = f"{repo_root()}/resources/addons/grafana/dashboards/jenkins-uptime.json"
    with open(path, encoding="utf-8") as f:
        d = json.load(f)
    out = []
    for v in d["templating"]["list"]:
        out.append((f"var ${v['name']}", v["query"]["query"]))
    for p in d["panels"]:
        for t in p.get("targets", []):
            out.append((f"{p['title']} [{t['refId']}]", t["expr"]))
    return out


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="cdpctl uptime-queries", description=__doc__)
    ap.add_argument("--url", help="Mimir query-frontend base URL (skips port-forward)")
    mode_g = ap.add_mutually_exclusive_group()
    mode_g.add_argument("--pre", action="store_true", help="force pre-deploy expectations")
    mode_g.add_argument("--post", action="store_true", help="force post-deploy expectations")
    _stage.add_output_flags(ap)
    args = ap.parse_args(argv)

    if not args.url:
        _stage.require("kubectl")

    base = args.url or port_forward()
    rules_src, rules = rule_exprs()

    deployed = False
    probe = api(base, "/api/v1/query", query='count(probe_success{job=~"jenkins-uptime-.*"})')
    if probe.get("status") == "success" and probe["data"]["result"]:
        deployed = True
    if args.pre:
        deployed = False
    if args.post:
        deployed = True

    out_mode = _stage.output_mode(args)
    if out_mode == "human":
        print(
            f"mode={'post-deploy' if deployed else 'pre-deploy'} "
            f"rules-from={rules_src} tenant={TENANT}\n"
        )

    checks = [(f"rule {n}", e) for n, e in rules] + dashboard_queries()
    rows, failures = [], 0
    for name, expr in checks:
        runnable = expr.replace("$master", ".*")
        m = re.fullmatch(r"label_values\((.+),\s*(\w+)\)", runnable)
        if m:
            res = api(base, f"/api/v1/label/{m.group(2)}/values", **{"match[]": m.group(1)})
            n = len(res.get("data", []))
        else:
            res = api(base, "/api/v1/query", query=runnable)
            n = len(res.get("data", {}).get("result", []))

        if res.get("status") != "success":
            status, note, bad = "PARSE-ERROR", str(res.get("error", ""))[:80], True
        elif n == 0:
            addon_only = bool(ADDON_SERIES.search(expr))
            if addon_only and not deployed:
                status, note, bad = "EMPTY", "expected pre-deploy (addon series)", False
            else:
                status, note, bad = "EMPTY", "UNEXPECTED: source data missing", True
        else:
            status, note, bad = "OK", "", False
        failures += bad
        rows.append([name, status, n, note])

    _stage.emit_rows(
        ["query", "status", "series", "note"],
        rows,
        out_mode,
        json_payload={
            "mode": "post-deploy" if deployed else "pre-deploy",
            "rules_from": rules_src,
            "tenant": TENANT,
            "failures": failures,
            "queries": [
                {"query": q, "status": s, "series": n, "note": note} for q, s, n, note in rows
            ],
        },
    )
    if out_mode == "human":
        print(f"\n{len(rows)} queries checked, {failures} failures")
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
