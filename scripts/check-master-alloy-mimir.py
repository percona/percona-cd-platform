#!/usr/bin/env python3
"""Verify every active Jenkins master is shipping metrics to Mimir via Alloy.

Masters are enumerated dynamically from repo source-of-truth (no hardcoded
list), classified by architecture:

  k8s         resources/jenkins/master/instances/<name>/   in-cluster controller
              (e.g. ps3-k8s); Mimir label master="<name>".
  tf-managed  terraform/master-<inst>.tf declaring the jenkins-master module
              (EKS-fronted on-demand EC2); Mimir label master="<inst>.cd".
  cf-managed  terraform/master-<inst>.tf WITHOUT that module and not already
              represented in-cluster; Mimir label master="<inst>.cd". (No
              master is in this state today; pg migrated to the module on
              2026-07-07.)

For each master it queries the in-cluster Mimir query-frontend for the gauge
every master pushes, hetzner_api_rate_limit_remaining, and reports the series
count plus seconds since the last sample. A fresh sample proves the master-side
Alloy is delivering through alloy-gateway into Mimir. A master whose newest
sample is older than --max-age, or absent entirely, is reported and the script
exits nonzero.

Read-only. Requires kubectl with context percona-ci-platform on PATH and a
reachable mimir-query-frontend. Run via: just check-master-alloy
"""
from __future__ import annotations

import argparse
import glob
import json
import os
import socket
import subprocess
import sys
import time
import urllib.parse
import urllib.request

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
CONTEXT = "percona-ci-platform"
TENANT = "percona-ci"
GAUGE = "hetzner_api_rate_limit_remaining"  # every master pushes this via Alloy

# Fenced clones run no Hetzner cloud, so they never push the gauge and are
# excluded from the freshness gate. Remove an entry when its clone is deleted.
FENCED = {"ps80-upgraded"}


def enumerate_masters() -> list[dict]:
    """Return [{inst, arch, label}] derived from repo source-of-truth."""
    masters: list[dict] = []

    # k8s: one directory per in-cluster controller instance. The directory name
    # is the Mimir master label verbatim (e.g. ps3-k8s).
    inst_dir = os.path.join(ROOT, "resources/jenkins/master/instances")
    k8s_names: list[str] = []
    if os.path.isdir(inst_dir):
        for name in sorted(os.listdir(inst_dir)):
            if os.path.isdir(os.path.join(inst_dir, name)):
                k8s_names.append(name)
                masters.append({"inst": name, "arch": "k8s", "label": name})

    # EC2 masters: terraform/master-<inst>.tf. Carrying the jenkins-master
    # module => tf-managed; otherwise it is a legacy non-module master. A master
    # already represented in-cluster (ps3 -> ps3-k8s) is skipped: its
    # master-*.tf is an ARM-fleet remnant, not a separate EC2 master.
    for tf in sorted(glob.glob(os.path.join(ROOT, "terraform", "master-*.tf"))):
        inst = os.path.basename(tf)[len("master-"):-len(".tf")]
        if inst in FENCED:
            continue
        if any(k == inst or k.startswith(inst + "-") for k in k8s_names):
            continue
        with open(tf, encoding="utf-8") as fh:
            body = fh.read()
        arch = "tf-managed" if "modules/jenkins-master" in body else "cf-managed"
        masters.append({"inst": inst, "arch": arch, "label": f"{inst}.cd"})

    return masters


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def port_forward(ns: str, svc: str, remote: int) -> tuple[subprocess.Popen, int]:
    local = _free_port()
    proc = subprocess.Popen(
        ["kubectl", "--context", CONTEXT, "-n", ns, "port-forward",
         f"svc/{svc}", f"{local}:{remote}"],
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
    )
    for _ in range(40):
        with socket.socket() as s:
            s.settimeout(0.3)
            if s.connect_ex(("127.0.0.1", local)) == 0:
                return proc, local
        time.sleep(0.25)
    proc.terminate()
    raise RuntimeError(f"port-forward to {ns}/{svc} did not become ready")


def mimir_query(port: int, query: str) -> dict:
    url = f"http://127.0.0.1:{port}/prometheus/api/v1/query?" + \
        urllib.parse.urlencode({"query": query})
    req = urllib.request.Request(url, headers={"X-Scope-OrgID": TENANT})
    with urllib.request.urlopen(req, timeout=20) as r:
        return json.load(r)


def by_master(resp: dict) -> dict[str, float]:
    out: dict[str, float] = {}
    for s in resp.get("data", {}).get("result", []):
        m = s.get("metric", {}).get("master")
        if m is None:
            continue
        try:
            out[m] = float(s["value"][1])
        except (KeyError, ValueError, TypeError):
            pass
    return out


def main() -> int:
    ap = argparse.ArgumentParser(
        description="Check every active master is shipping to Mimir via Alloy.")
    ap.add_argument("--max-age", type=int, default=600,
                    help="seconds since last sample before a master is STALE (default 600)")
    ap.add_argument("--json", action="store_true", help="JSON output")
    args = ap.parse_args()

    masters = enumerate_masters()
    if not masters:
        print("no masters found in repo source-of-truth", file=sys.stderr)
        return 1

    proc, port = port_forward("mimir", "mimir-query-frontend", 8080)
    try:
        series = by_master(mimir_query(port, f"count by (master) ({GAUGE})"))
        fresh = by_master(mimir_query(port, f"time() - max by (master) (timestamp({GAUGE}))"))
    finally:
        proc.terminate()
        try:
            proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            proc.kill()

    rows = []
    for m in masters:
        cnt = series.get(m["label"])
        age = fresh.get(m["label"])
        if cnt is None:
            status = "MISSING"
        elif age is not None and age > args.max_age:
            status = "STALE"
        else:
            status = "OK"
        rows.append({**m, "status": status,
                     "series": int(cnt) if cnt is not None else None,
                     "age_s": int(age) if age is not None else None})

    extra = sorted(set(series) - {m["label"] for m in masters})

    if args.json:
        print(json.dumps({"masters": rows, "unexpected_senders": extra}, indent=2))
    else:
        order = {"k8s": 0, "tf-managed": 1, "cf-managed": 2}
        rows.sort(key=lambda r: (order.get(r["arch"], 9), r["inst"]))
        w = max((len(r["label"]) for r in rows), default=12) + 2
        print(f"{'ARCH':<11} {'MASTER':<{w}} {'STATUS':<8} {'SERIES':<7} AGE_S")
        for r in rows:
            print(f"{r['arch']:<11} {r['label']:<{w}} {r['status']:<8} "
                  f"{str(r['series']) if r['series'] is not None else '-':<7} "
                  f"{r['age_s'] if r['age_s'] is not None else '-'}")
        ok = sum(r["status"] == "OK" for r in rows)
        print(f"\n{ok}/{len(rows)} masters delivering to Mimir via Alloy (threshold {args.max_age}s)")
        if extra:
            print(f"unexpected Mimir senders not in repo: {', '.join(extra)}")

    bad = [r["label"] for r in rows if r["status"] != "OK"]
    if bad:
        print(f"\nFAIL not delivering: {', '.join(bad)}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
