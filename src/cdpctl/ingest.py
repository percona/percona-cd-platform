#!/usr/bin/env python3
"""Per-master Mimir + Loki ingest probe.

Reads from the in-cluster query-frontends (no Grafana auth needed) to verify
every Jenkins master (enumerated from repo source-of-truth, shared with
`cdpctl alloy`) is currently shipping metrics + logs through
alloy-gateway. Output is one row per master: Mimir series count, Mimir
freshness (seconds since last sample), Mimir distinct metric-name cardinality,
Loki log-line count over the lookback window. Quiet masters can show 0 in
narrow windows; widen with `--lookback 24h` to cross-check.

Read-only. Exits 0 if every expected master is present in Mimir, nonzero if
any master is missing (Loki gaps are reported but don't fail; quiet masters
legitimately have 0 lines in narrow windows). Output: kubectl-style table
(default), --json envelope, or --llm pipe-delimited rows.

Requires kubectl on PATH and a kubeconfig with context `percona-ci-platform`.
"""

from __future__ import annotations

import argparse
import math
import socket
import subprocess
import sys
import time
import urllib.parse
from typing import Any

from cdpctl import _stage
from cdpctl._repo import http_json
from cdpctl.alloy import enumerate_masters

CONTEXT = "percona-ci-platform"
TENANT = "percona-ci"

# Pick local ports unlikely to clash with anything else.
MIMIR_PORT = 58080
LOKI_PORT = 53100


def port_forward(ns: str, svc: str, local: int, remote: int) -> subprocess.Popen:
    """Background `kubectl port-forward` and wait for the local port to accept."""
    proc = subprocess.Popen(
        [
            "kubectl",
            "--context",
            CONTEXT,
            "-n",
            ns,
            "port-forward",
            f"svc/{svc}",
            f"{local}:{remote}",
        ],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    for _ in range(40):
        with socket.socket() as s:
            s.settimeout(0.3)
            if s.connect_ex(("127.0.0.1", local)) == 0:
                return proc
        time.sleep(0.25)
    proc.terminate()
    raise RuntimeError(f"port-forward to {ns}/{svc} did not become ready")


def _instant_query(port: int, path: str, query: str) -> Any:
    url = f"http://127.0.0.1:{port}{path}?" + urllib.parse.urlencode({"query": query})
    return http_json(url, headers={"X-Scope-OrgID": TENANT})


def mimir_query(query: str) -> Any:
    return _instant_query(MIMIR_PORT, "/prometheus/api/v1/query", query)


def loki_query(query: str) -> Any:
    return _instant_query(LOKI_PORT, "/loki/api/v1/query", query)


def by_master(resp: Any) -> dict[str, Any]:
    """{master: value} from an instant-query response (the jq `lookup` def).

    Values stay the raw API strings (jq took `.value[1]` verbatim), so JSON
    and CSV output keep the bash script's string-typed cells.
    """
    out: dict[str, Any] = {}
    data = resp.get("data") if isinstance(resp, dict) else None
    results = data.get("result") if isinstance(data, dict) else None
    for r in results or []:
        master = (r.get("metric") or {}).get("master")
        if master is None:
            continue
        value = r.get("value")
        out[master] = value[1] if isinstance(value, list) and len(value) > 1 else None
    return out


def build_records(
    series: dict[str, Any],
    fresh: dict[str, Any],
    card: dict[str, Any],
    loki: dict[str, Any],
) -> list[dict[str, Any]]:
    """One flat record per expected master; absent values stay null."""
    # Masters enumerated dynamically from repo source-of-truth (shared with
    # `cdpctl alloy`), so a migrated controller (e.g. ps3 -> ps3-k8s) never
    # leaves a stale expectation behind.
    return [
        {
            "master": m["label"],
            "mimir_series": series.get(m["label"]),
            "mimir_freshness_s": fresh.get(m["label"]),
            "mimir_cardinality": card.get(m["label"]),
            "loki_lines": loki.get(m["label"]),
        }
        for m in sorted(enumerate_masters(), key=lambda x: str(x["label"]))
    ]


def _fresh_cell(v: Any) -> str:
    """Table freshness column: null -> "-", else jq tonumber|floor|tostring."""
    if v is None:
        return "-"
    return str(math.floor(float(v)))


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(
        prog="cdpctl ingest",
        description=(
            "Per-master Mimir + Loki ingest probe: verifies every Jenkins master is "
            "shipping metrics + logs through alloy-gateway by reading the in-cluster "
            "query-frontends. Exits 0 if every expected master is present in Mimir, "
            "1 if any is missing (Loki gaps are reported but don't fail)."
        ),
    )
    ap.add_argument(
        "--lookback",
        default="1h",
        help="Loki count_over_time window, e.g. 1h or 24h (default 1h)",
    )
    _stage.add_output_flags(ap)
    args = ap.parse_args(argv)

    _stage.require("kubectl")

    procs: list[subprocess.Popen] = []
    try:
        procs.append(port_forward("mimir", "mimir-query-frontend", MIMIR_PORT, 8080))
        procs.append(port_forward("loki", "loki-query-frontend", LOKI_PORT, 3100))

        # 1. Series count per master from a known-emitted gauge (every master pushes this).
        series = by_master(mimir_query("count by (master) (hetzner_api_rate_limit_remaining)"))
        # 2. Seconds since last sample per master.
        fresh = by_master(
            mimir_query("time() - max by (master) (timestamp(hetzner_api_rate_limit_remaining))")
        )
        # 3. Distinct metric-name cardinality per master (event-driven counters reflect here).
        card = by_master(
            mimir_query('count by (master) (group by (master, __name__) ({master=~".+"}))')
        )
        # 4. Loki log-line count per master over the lookback window.
        loki = by_master(
            loki_query(
                f'sum by (master) (count_over_time({{fleet="percona-jenkins"}}[{args.lookback}]))'
            )
        )
    finally:
        for p in procs:
            p.terminate()
        for p in procs:
            try:
                p.wait(timeout=5)
            except subprocess.TimeoutExpired:
                p.kill()

    records = build_records(series, fresh, card, loki)

    mode = _stage.output_mode(args)
    if mode == "human":
        print(f"lookback={args.lookback} tenant={TENANT}\n")
    rows = [
        [
            str(r["master"]),
            str(r["mimir_series"]) if r["mimir_series"] is not None else "MISSING",
            _fresh_cell(r["mimir_freshness_s"]),
            str(r["mimir_cardinality"]) if r["mimir_cardinality"] is not None else "-",
            str(r["loki_lines"]) if r["loki_lines"] is not None else "-",
        ]
        for r in records
    ]
    _stage.emit_rows(
        ["master", "mimir_series", "freshness_s", "cardinality", "loki_lines"],
        rows,
        mode,
        json_payload={"lookback": args.lookback, "tenant": TENANT, "masters": records},
    )

    # Exit nonzero if any expected master is missing from Mimir.
    missing = [str(r["master"]) for r in records if r["mimir_series"] is None]
    if missing:
        print(f"\nMISSING from Mimir: {','.join(missing)}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
