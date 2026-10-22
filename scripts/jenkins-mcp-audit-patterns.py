#!/usr/bin/env -S uv run --script
# /// script
# requires-python = ">=3.11"
# dependencies = []
# ///
"""jenkins-mcp-audit-patterns.py - tool-calling patterns from the jenkins-mcp audit log in Loki.

Exports every `mcp_tool_call` audit line for the window, dedupes on request_id (the pod log
source can ingest a line more than once, so raw line counts overcount), then groups calls into
sessions (same user, gaps under 15 minutes) and counts the patterns that cost avoidable calls:

  fan-out     same tool + args on 3+ masters within 5 minutes (a cross-master lookup by hand)
  repeats     identical call (tool, master, args) more than once in a session
  walks       one job, 4+ distinct build numbers read in a session
  polling     same call on one build 3+ times, each at least 20 s apart
  discovery   get_queue_item / get_build_history within 5 minutes after a build_item
  grep-chain  grep_build_artifact on one build, same pattern, 3+ distinct paths
  errors      by tool, status and error class

Read-only. Prints kubectl-style tables; --jsonl also writes the deduped export.

Usage:
  scripts/jenkins-mcp-audit-patterns.py                 # last 30 days
  scripts/jenkins-mcp-audit-patterns.py --days 14 --jsonl /tmp/audit.jsonl

Options:
  --loki-url URL   Loki base (default: own port-forward to svc/loki-query-frontend :3100)
  --days N         window length in days (default 30, Loki retention is 30)
  --jsonl PATH     also write the deduped audit records, one JSON object per line
"""

import argparse
import atexit
import collections
import json
import socket
import subprocess
import sys
import time
import urllib.parse
import urllib.request
from datetime import datetime

CONTEXT = "percona-ci-platform"
PF_PORT = 13109
QUERY = '{instance=~"jenkins-mcp/.+"} |= "mcp_tool_call"'
PAGE = 5000
WINDOW_S = 6 * 3600
SESSION_GAP_S = 15 * 60


def fail(msg: str) -> None:
    print(f"precondition: {msg}", file=sys.stderr)
    sys.exit(2)


def port_forward() -> str:
    proc = subprocess.Popen(
        ["kubectl", "--context", CONTEXT, "-n", "loki", "port-forward",
         "svc/loki-query-frontend", f"{PF_PORT}:3100"],
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


def export(loki: str, days: int) -> tuple[list[dict], int]:
    """All audit records in the window, deduped on request_id, plus the raw line count."""
    end = int(time.time())
    start = end - days * 86400
    seen: dict[str, dict] = {}
    raw = 0
    t = start
    while t < end:
        window_end = min(t + WINDOW_S, end)
        page_end = window_end
        while True:
            qs = urllib.parse.urlencode({
                "query": QUERY, "start": f"{t}000000000", "end": f"{page_end}000000000",
                "limit": PAGE, "direction": "backward",
            })
            with urllib.request.urlopen(f"{loki}/loki/api/v1/query_range?{qs}", timeout=60) as resp:
                data = json.load(resp)
            values = [v for s in data["data"]["result"] for v in s["values"]]
            for _, line in values:
                try:
                    record = json.loads(line)
                except ValueError:
                    continue
                if record.get("event") != "mcp_tool_call":
                    continue
                raw += 1
                seen[record.get("request_id") or line] = record
            if len(values) < PAGE:
                break
            page_end = min(int(v[0]) for v in values) // 1_000_000_000
        t = window_end
    return sorted(seen.values(), key=lambda r: r["ts"]), raw


def prepare(rows: list[dict]) -> dict[int, list[dict]]:
    """Normalise user, master and args on each record and group records into sessions."""
    for r in rows:
        r["t"] = datetime.fromisoformat(r["ts"]).timestamp()
        r["u"] = (r.get("preferred_username") or r.get("sub") or "?").split("@")[0]
        args = dict(r.get("args") or {})
        r["m"] = r.get("master") or args.get("master") or "default"
        args.pop("master", None)
        r["a"] = args
        r["akey"] = json.dumps(args, sort_keys=True)
    rows.sort(key=lambda r: (r["u"], r["t"]))
    sessions: dict[int, list[dict]] = collections.defaultdict(list)
    sid, prev = 0, None
    for r in rows:
        if prev is None or prev["u"] != r["u"] or r["t"] - prev["t"] > SESSION_GAP_S:
            sid += 1
        r["s"] = sid
        sessions[sid].append(r)
        prev = r
    return sessions


def table(title: str, header: list[str], data: list[list]) -> None:
    print(f"\n{title}")
    if not data:
        print("  (none)")
        return
    widths = [max(len(str(x)) for x in col) for col in zip(header, *data)]
    for line in [header, *data]:
        print("  " + "  ".join(str(x).ljust(w) for x, w in zip(line, widths)).rstrip())


def build_key(r: dict) -> tuple | None:
    a = r["a"]
    if "fullname" in a and "number" in a:
        return (r["m"], a["fullname"], str(a["number"]))
    return None


def report(rows: list[dict], raw: int, sessions: dict[int, list[dict]]) -> None:
    users = collections.Counter(r["u"] for r in rows)
    print(f"calls={len(rows)} raw_lines={raw} duplicate_lines={raw - len(rows)} "
          f"users={len(users)} sessions={len(sessions)} "
          f"window={min(r['ts'] for r in rows)[:10]}..{max(r['ts'] for r in rows)[:10]}")

    data = []
    for user, n in users.most_common():
        ur = [r for r in rows if r["u"] == user]
        nsess = len({r["s"] for r in ur})
        top = ",".join(f"{t}:{c}" for t, c in collections.Counter(r["tool"] for r in ur).most_common(3))
        data.append([user, n, len({r["ts"][:10] for r in ur}), nsess, round(n / nsess, 1),
                     sum(r["status"] != "ok" for r in ur), top])
    table("PER USER", ["USER", "CALLS", "DAYS", "SESSIONS", "PER_SESSION", "ERRORS", "TOP_TOOLS"], data)

    patterns = []
    fan = repeats = walks = walk_calls = poll = grep_chains = grep_calls = 0
    for s in sessions.values():
        by_args = collections.defaultdict(list)
        for r in s:
            by_args[(r["tool"], r["akey"])].append(r)
        for rs in by_args.values():
            if len({r["m"] for r in rs}) >= 3 and rs[-1]["t"] - rs[0]["t"] < 300:
                fan += len(rs)
        for n in collections.Counter((r["tool"], r["m"], r["akey"]) for r in s).values():
            repeats += max(0, n - 1)
        jobs = collections.defaultdict(set)
        job_calls = collections.Counter()
        for r in s:
            key = build_key(r)
            if key:
                jobs[key[:2]].add(key[2])
                job_calls[key[:2]] += 1
        for job, numbers in jobs.items():
            if len(numbers) >= 4:
                walks += 1
                walk_calls += job_calls[job]
        per_build = collections.defaultdict(list)
        for r in s:
            if build_key(r):
                per_build[(build_key(r), r["tool"], r["akey"])].append(r["t"])
        for ts in per_build.values():
            if len(ts) >= 3 and all(b - a >= 20 for a, b in zip(ts, ts[1:])):
                poll += len(ts)
        greps = collections.defaultdict(set)
        grep_n = collections.Counter()
        for r in s:
            if r["tool"] == "grep_build_artifact" and build_key(r):
                k = (build_key(r), r["a"].get("pattern"))
                greps[k].add(r["a"].get("relative_path"))
                grep_n[k] += 1
        for k, paths in greps.items():
            if len(paths) >= 3:
                grep_chains += 1
                grep_calls += grep_n[k]
    discovery = 0
    ordered = sorted(rows, key=lambda r: r["t"])
    for i, r in enumerate(ordered):
        if r["tool"] != "build_item" or r["status"] != "ok":
            continue
        for x in ordered[i + 1:i + 40]:
            if x["t"] - r["t"] > 300:
                break
            if x["u"] != r["u"]:
                continue
            if x["tool"] == "get_queue_item" or (
                x["tool"] == "get_build_history" and x["a"].get("fullname") == r["a"].get("fullname")
            ):
                discovery += 1
    patterns += [
        ["fan-out", fan, "find_items"],
        ["repeats", repeats, "get_build_walk, wait_for_build"],
        ["walks", f"{walk_calls} in {walks}", "get_build_walk"],
        ["polling", poll, "wait_for_build"],
        ["discovery", discovery, "wait_for_build"],
        ["grep-chain", f"{grep_calls} in {grep_chains}", "grep_build_artifact glob"],
    ]
    table("PATTERNS (calls, overlapping)", ["PATTERN", "CALLS", "TOOL_THAT_COLLAPSES_IT"], patterns)

    errors = collections.Counter((r["tool"], r["status"], r.get("error") or "-") for r in rows if r["status"] != "ok")
    table("ERRORS", ["TOOL", "STATUS", "CLASS", "COUNT"],
          [[t, s, e, n] for (t, s, e), n in errors.most_common(15)])

    tools = collections.Counter(r["tool"] for r in rows)
    table("TOOLS", ["TOOL", "CALLS"], [[t, n] for t, n in tools.most_common()])


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--loki-url", help="Loki base URL (skips port-forward)")
    ap.add_argument("--days", type=int, default=30)
    ap.add_argument("--jsonl", help="write the deduped audit records to this path")
    args = ap.parse_args()

    rows, raw = export(args.loki_url or port_forward(), args.days)
    if not rows:
        fail("no mcp_tool_call audit lines in the window")
    if args.jsonl:
        with open(args.jsonl, "w") as f:
            for r in rows:
                f.write(json.dumps(r) + "\n")
    report(rows, raw, prepare(rows))


if __name__ == "__main__":
    main()
