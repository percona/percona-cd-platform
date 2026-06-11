#!/usr/bin/env python3
"""End-to-end health check of the LGTM push pipeline for one Jenkins master.

Walks the push pipeline stage by stage: prerequisites (cluster reachability,
alloy-gateway bearer fetch), SSH-dependent master-side checks (Hetzner plugin
loopback, Alloy systemd push deltas), ALB + bearer enforcement, alloy-gateway
pods, Mimir, Loki (including a deterministic probe round-trip), and Grafana.

All 10 masters run the master-side Alloy systemd unit
(`Percona-Lab/jenkins-pipelines` PR #4037). For fleet-wide ingest spot-checks
without SSH use `cdpctl alloy`; this walk remains the deepest per-master
check, including the SSH-dependent master-side stages.

Output: human-readable stage rows by default; `--json` prints a single JSON
envelope, `--llm` prints pipe-delimited `status|label|detail` rows.

Read-only. Exits 0 if every stage is green, 1 on any FAIL, 2 on precondition
(missing tool, unusable kubectl context, bad usage).

Required on PATH always: aws, kubectl, dig. Required when master stages are
enabled: ssh. Requires a kubeconfig context for the LGTM cluster (default
`percona-ci-platform`; override with $KUBE_CONTEXT). The bearer fetch uses
$AWS_PROFILE; when it is not exported, the fetch and the bearer-dependent
stages SKIP. Optional: $GRAFANA_TOKEN for the Grafana datasource + proxy
probes (otherwise the script tries the `grafana` admin secret in-cluster; if
neither is available those probes SKIP).
"""

from __future__ import annotations

import argparse
import base64
import http.client
import json
import math
import os
import re
import socket
import subprocess
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import UTC, datetime, timedelta
from typing import Any

from cdpctl import _stage

SECRET_ID = "percona-ci-platform/alloy-gateway/bearer"
SECRET_REGION = "us-east-1"
SSH_USER = "ec2-user"

# Remote script for the Hetzner plugin loopback probe (runs on the master via
# `bash -s <inst>`; raw string so the curl line-continuation backslash and the
# nested heredocs survive verbatim).
_REMOTE_PLUGIN_PROBE = r"""set -u
inst="$1"
tmp=$(mktemp)
trap 'rm -f "$tmp"' EXIT
probe=$(curl -sLS -m 5 -o "$tmp" -w '%{http_code} %{size_download}' \
  http://127.0.0.1:8080/hetzner-prometheus/)
echo "PROBE=${probe}"
ver=$(sudo python3 - "$inst" <<'PY' 2>/dev/null
import sys, zipfile
inst = sys.argv[1]
try:
    with zipfile.ZipFile(f"/mnt/{inst}.cd.percona.com/plugins/hetzner-cloud.jpi") as z:
        with z.open("META-INF/MANIFEST.MF") as f:
            for line in f.read().decode(errors="replace").splitlines():
                if line.startswith("Plugin-Version:"):
                    print(line.split(":", 1)[1].strip())
                    break
except Exception:
    pass
PY
)
echo "VERSION=${ver:-unknown}"
fam=$(grep -c '^# HELP ' "$tmp" || true)
echo "FAMILIES=${fam:-0}"
"""

# Remote script for the master-side Alloy systemd check: sample
# localhost:12345/metrics twice ~12s apart so we report deltas, not lifetime
# counters (a historical blip used to peg failed_total > 0 forever).
_REMOTE_ALLOY_PROBE = r"""set -u
state=$(systemctl is-active alloy 2>/dev/null || echo unknown)
echo "STATE=${state}"
echo "----SNAPSHOT-A----"
curl -sS -m 5 http://127.0.0.1:12345/metrics 2>/dev/null || true
echo "----SLEEP----"
sleep 12
echo "----SNAPSHOT-B----"
curl -sS -m 5 http://127.0.0.1:12345/metrics 2>/dev/null || true
"""


def _k(ctx: str, *args: str) -> subprocess.CompletedProcess[str]:
    """Pinned kubectl wrapper (stderr discarded, like the bash 2>/dev/null)."""
    return subprocess.run(
        ["kubectl", "--context", ctx, *args],
        stdout=subprocess.PIPE,
        stderr=subprocess.DEVNULL,
        text=True,
        check=False,
    )


def _http_status(
    url: str,
    *,
    method: str = "GET",
    headers: dict[str, str] | None = None,
    data: bytes | None = None,
    timeout: float = 5.0,
) -> str:
    """Status code as a string; '000' on transport failure (curl -w semantics)."""
    req = urllib.request.Request(url, data=data, headers=headers or {}, method=method)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            resp.read()
            return str(resp.status)
    except urllib.error.HTTPError as e:
        e.close()
        return str(e.code)
    except (OSError, http.client.HTTPException):
        return "000"


def _http_text(url: str, *, headers: dict[str, str] | None = None, timeout: float = 5.0) -> str:
    """Response body as text; error bodies pass through (curl without -f), '' on failure."""
    req = urllib.request.Request(url, headers=headers or {})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return resp.read().decode("utf-8", errors="replace")
    except urllib.error.HTTPError as e:
        try:
            return e.read().decode("utf-8", errors="replace")
        except OSError:
            return ""
        finally:
            e.close()
    except (OSError, http.client.HTTPException):
        return ""


def _json_loads(text: str) -> Any:
    try:
        return json.loads(text)
    except ValueError:
        return None


def _jq_str(text: str, *path: str | int) -> str:
    """`jq -r '.a.b[0] // empty'` equivalent: '' on missing/null/false/non-JSON."""
    cur = _json_loads(text)
    for key in path:
        if isinstance(key, int):
            if not isinstance(cur, list) or key >= len(cur):
                return ""
            cur = cur[key]
        else:
            if not isinstance(cur, dict):
                return ""
            cur = cur.get(key)
    if cur is None or cur is False:
        return ""
    return str(cur)


def _metric_sum(text: str, name: str) -> int:
    """Sum the value column of every series of one metric family.

    Mirrors the bash metric_sum awk: line must start with the name followed by
    `{` or whitespace (so prefix-shared metric names do not cross-contaminate);
    the value is the last whitespace field (Alloy/Mimir/Loki emit no timestamps).
    """
    total = 0.0
    for line in text.splitlines():
        if line.startswith("#") or not line.startswith(name):
            continue
        nxt = line[len(name) : len(name) + 1]
        if nxt not in ("{", " ", "\t"):
            continue
        fields = line.split()
        if not fields:
            continue
        try:
            v = float(fields[-1])
        except ValueError:
            continue
        if math.isfinite(v):
            total += v
    return int(round(total))


def _first_prefixed(lines: list[str], prefix: str) -> str:
    """Value of the first `PREFIX=value` line ('' when absent)."""
    for line in lines:
        if line.startswith(prefix):
            return line[len(prefix) :]
    return ""


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def _pf_stop(proc: subprocess.Popen[bytes]) -> None:
    proc.terminate()
    try:
        proc.wait(timeout=5)
    except subprocess.TimeoutExpired:
        proc.kill()


def _port_forward(
    ctx: str, ns: str, target: str, remote: int
) -> tuple[subprocess.Popen[bytes], int] | None:
    """Background kubectl port-forward; None when it never becomes ready.

    The bash pf_start contract: on failure the caller MUST treat the dependent
    probe as unrunnable (never silently reuse a stale forward on a fixed port).
    """
    local = _free_port()
    proc = subprocess.Popen(
        ["kubectl", "--context", ctx, "-n", ns, "port-forward", target, f"{local}:{remote}"],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    for _ in range(40):
        if proc.poll() is not None:
            return None
        with socket.socket() as s:
            s.settimeout(0.3)
            if s.connect_ex(("127.0.0.1", local)) == 0:
                return proc, local
        time.sleep(0.25)
    _pf_stop(proc)
    return None


def _finalize(st: _stage.Stages, inst: str, ctx: str, mode: str, code: int) -> int:
    """Emit the end-of-run payload for the active output mode."""
    if mode == "json":
        print(json.dumps(st.envelope(master=inst, context=ctx), indent=2))
        return code
    if mode == "llm":
        st.emit_llm()
        return code
    print("\n== Summary ==")
    print(f"  master={inst}  context={ctx}  pass={st.passed}  fail={st.failed}  skip={st.skipped}")
    if st.failed > 0 or st.skipped > 0:
        print("\n== Issues ==")
        st.reprint_issues()
    return code


def _stage0(st: _stage.Stages, ctx: str, profile: str) -> tuple[bool, str]:
    """Prerequisites: cluster reachability + bearer fetch. Returns (cluster_ok, bearer)."""
    st.section("Stage 0: prerequisites")

    if _k(ctx, "get", "ns", "alloy-gateway").returncode == 0:
        st.ok("kubectl context reaches cluster", f"ctx={ctx}")
    else:
        st.fail(
            "kubectl context reaches cluster",
            f"context={ctx}; namespace alloy-gateway not found",
        )
        return False, ""

    # Bearer fetch: separate operator-precondition (AWS auth) from production
    # config error (secret exists but .bearer_token missing). No AWS_PROFILE is
    # the same operator-precondition class as a failed AWS read: the bearer is
    # only needed by the push/round-trip probes, so SKIP those instead of dying.
    bearer = ""
    if not profile:
        st.skip(
            "fetched alloy-gateway bearer from AWS SM",
            "AWS_PROFILE not exported; bearer-dependent stages will SKIP",
        )
        return True, bearer
    res = subprocess.run(
        [
            "aws",
            "secretsmanager",
            "get-secret-value",
            "--secret-id",
            SECRET_ID,
            "--region",
            SECRET_REGION,
            "--profile",
            profile,
            "--query",
            "SecretString",
            "--output",
            "text",
        ],
        stdout=subprocess.PIPE,
        stderr=subprocess.DEVNULL,
        text=True,
        check=False,
    )
    if res.returncode == 0:
        parsed = _json_loads(res.stdout.rstrip("\n"))
        tok = parsed.get("bearer_token") if isinstance(parsed, dict) else None
        bearer = "" if tok in (None, False) else str(tok)
        if bearer and bearer != "null":
            st.ok("fetched alloy-gateway bearer from AWS SM", f"len={len(bearer)}")
        else:
            st.fail(
                "fetched alloy-gateway bearer from AWS SM",
                "secret exists but .bearer_token missing/null (production config error)",
            )
            bearer = ""
    else:
        st.skip(
            "fetched alloy-gateway bearer from AWS SM",
            f"AWS SM read failed (profile={profile}); bearer-dependent stages will SKIP",
        )
    return True, bearer


def _stage_master(st: _stage.Stages, inst: str, host: str, ssh_key: str, skip_master: bool) -> None:
    """Stages 1+2: SSH-dependent master-side checks."""
    if skip_master:
        st.section("Stage 1+2: master-side checks (skipped)")
        st.skip("plugin loopback", "--skip-master")
        st.skip("alloy systemd", "--skip-master")
        return
    if not os.access(ssh_key, os.R_OK):
        st.section("Stage 1+2: master-side checks (skipped)")
        st.skip("plugin loopback", f"ssh key not present on this host ({ssh_key})")
        st.skip("alloy systemd", f"ssh key not present on this host ({ssh_key})")
        return

    st.section(f"Stage 1: SSH transport to master ({host})")
    ssh_base = [
        "ssh",
        "-i",
        ssh_key,
        "-o",
        "ConnectTimeout=8",
        "-o",
        "BatchMode=yes",
        f"{SSH_USER}@{host}",
    ]
    transport = subprocess.run(
        [*ssh_base, "true"],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        check=False,
    )
    if transport.returncode == 0:
        st.ok(f"ssh transport to {host}", "")
    else:
        st.fail(
            f"ssh transport to {host}",
            "ssh key present but connect failed; loopback probes will SKIP",
        )
        # ssh transport already FAILed; do not also fail dependent probes.
        st.skip("plugin loopback /hetzner-prometheus/", "ssh transport unavailable")
        st.skip("alloy systemd push state", "ssh transport unavailable")
        return

    st.section(f"Stage 1: Hetzner plugin endpoint ({host})")
    res = subprocess.run(
        [*ssh_base, f"bash -s {inst}"],
        input=_REMOTE_PLUGIN_PROBE,
        stdout=subprocess.PIPE,
        stderr=subprocess.DEVNULL,
        text=True,
        check=False,
    )
    if res.returncode == 0:
        lines = res.stdout.splitlines()
        probe_parts = _first_prefixed(lines, "PROBE=").split()
        http_code = probe_parts[0] if probe_parts else ""
        nbytes = probe_parts[1] if len(probe_parts) > 1 else ""
        plugin_ver = _first_prefixed(lines, "VERSION=")
        families = _first_prefixed(lines, "FAMILIES=")
        try:
            families_n = int(families)
        except ValueError:
            families_n = 0
        if http_code == "200" and families_n >= 1:
            st.ok(
                "plugin loopback /hetzner-prometheus/",
                f"v={plugin_ver or '?'} HTTP={http_code} bytes={nbytes} families={families}",
            )
        else:
            st.fail(
                "plugin loopback /hetzner-prometheus/",
                f"HTTP={http_code} bytes={nbytes} families={families} v={plugin_ver or '?'}",
            )
    else:
        st.fail("plugin loopback /hetzner-prometheus/", f"ssh probe command failed on {host}")

    st.section(f"Stage 2: Master Alloy systemd ({host})")
    res = subprocess.run(
        [*ssh_base, "bash -s"],
        input=_REMOTE_ALLOY_PROBE,
        stdout=subprocess.PIPE,
        stderr=subprocess.DEVNULL,
        text=True,
        check=False,
    )
    if res.returncode == 0:
        lines = res.stdout.splitlines()
        state = _first_prefixed(lines, "STATE=")
        snap_a: list[str] = []
        snap_b: list[str] = []
        in_a = in_b = False
        for line in lines:
            if line == "----SNAPSHOT-A----":
                in_a = True
                continue
            if line == "----SLEEP----":
                in_a = False
                continue
            if line == "----SNAPSHOT-B----":
                in_b = True
                continue
            if in_b:
                snap_b.append(line)
            elif in_a:
                snap_a.append(line)
        a_text, b_text = "\n".join(snap_a), "\n".join(snap_b)
        d_sent = _metric_sum(b_text, "prometheus_remote_storage_samples_total") - _metric_sum(
            a_text, "prometheus_remote_storage_samples_total"
        )
        d_fail = _metric_sum(b_text, "prometheus_remote_storage_samples_failed_total") - (
            _metric_sum(a_text, "prometheus_remote_storage_samples_failed_total")
        )
        d_drop = _metric_sum(b_text, "prometheus_remote_storage_samples_dropped_total") - (
            _metric_sum(a_text, "prometheus_remote_storage_samples_dropped_total")
        )
        d_loki = _metric_sum(b_text, "loki_write_sent_entries_total") - _metric_sum(
            a_text, "loki_write_sent_entries_total"
        )
        pend_b = _metric_sum(b_text, "prometheus_remote_storage_samples_pending")
        if state == "active" and d_sent > 0 and d_fail == 0 and d_drop == 0:
            st.ok(
                "alloy systemd push state",
                f"active dsent={d_sent} dfailed={d_fail} ddropped={d_drop} "
                f"dloki={d_loki} pending={pend_b}",
            )
        else:
            st.fail(
                "alloy systemd push state",
                f"state={state} dsent={d_sent} dfailed={d_fail} ddropped={d_drop} "
                f"dloki={d_loki} pending={pend_b}",
            )
    else:
        st.fail("alloy systemd push state", f"ssh probe command failed on {host}")


def _stage3(st: _stage.Stages, inst: str, bearer: str) -> tuple[str, str]:
    """ALB + bearer enforcement. Returns (now_ns, probe_label) for the Stage 6 round-trip."""
    st.section("Stage 3: ALB + bearer enforcement")
    specs = [
        ("mimir-push.cd.percona.com", "/api/v1/metrics/write", "application/x-protobuf"),
        ("loki-push.cd.percona.com", "/loki/api/v1/push", "application/json"),
        ("tempo-push.cd.percona.com", "/v1/traces", "application/x-protobuf"),
    ]
    for hostname, pathpart, ct in specs:
        dig = subprocess.run(
            ["dig", "+short", hostname],
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            text=True,
            check=False,
        )
        dig_lines = dig.stdout.splitlines()
        ip = dig_lines[0] if dig_lines else ""
        anon = _http_status(
            f"https://{hostname}{pathpart}",
            method="POST",
            headers={"Content-Type": ct},
            data=b"",
            timeout=5,
        )
        if anon == "401":
            st.ok(f"{hostname} anonymous POST rejected", f"ip={ip} HTTP=401")
        else:
            st.fail(f"{hostname} anonymous POST rejected", f"ip={ip} HTTP={anon} (expected 401)")

    now_ns = f"{int(time.time())}000000000"
    probe_label = f"verify-{inst}-{now_ns}"
    if bearer:
        payload = json.dumps(
            {
                "streams": [
                    {
                        "stream": {"probe": probe_label},
                        "values": [[now_ns, "verify-observability probe"]],
                    }
                ]
            },
            separators=(",", ":"),
        )
        auth = _http_status(
            "https://loki-push.cd.percona.com/loki/api/v1/push",
            method="POST",
            headers={"Authorization": f"Bearer {bearer}", "Content-Type": "application/json"},
            data=payload.encode(),
            timeout=8,
        )
        if auth == "204":
            st.ok("loki-push bearer accepted", "HTTP=204")
        else:
            st.fail("loki-push bearer accepted", f"HTTP={auth} (expected 204)")

        # Tempo coverage: until real traces exist, at least assert that an
        # authenticated POST with an empty body is no longer rejected as
        # unauthenticated (any 2xx/4xx/5xx other than 401 satisfies the auth
        # gate). 401 here would mean the bearer-auth sidecar is rejecting
        # valid credentials for the tempo path.
        tanon = _http_status(
            "https://tempo-push.cd.percona.com/v1/traces",
            method="POST",
            headers={
                "Authorization": f"Bearer {bearer}",
                "Content-Type": "application/x-protobuf",
            },
            data=b"",
            timeout=8,
        )
        if tanon != "401" and tanon:
            st.ok("tempo-push bearer not 401", f"HTTP={tanon}")
        else:
            st.fail("tempo-push bearer not 401", f"HTTP={tanon} (bearer rejected)")
    else:
        st.skip("loki-push bearer accepted", "no bearer fetched")
        st.skip("tempo-push bearer not 401", "no bearer fetched")
    return now_ns, probe_label


def _stage4(st: _stage.Stages, ctx: str) -> None:
    """alloy-gateway pod state."""
    st.section("Stage 4: alloy-gateway pods")
    res = _k(ctx, "-n", "alloy-gateway", "get", "deploy", "alloy-gateway", "-o", "json")
    deploy = _json_loads(res.stdout if res.returncode == 0 and res.stdout else "{}")
    deploy = deploy if isinstance(deploy, dict) else {}
    desired = (deploy.get("spec") or {}).get("replicas") or 0
    ready = (deploy.get("status") or {}).get("readyReplicas") or 0
    avail = ""
    for cond in (deploy.get("status") or {}).get("conditions") or []:
        if isinstance(cond, dict) and cond.get("type") == "Available":
            status = cond.get("status")
            avail = "null" if status is None else str(status)
            break
    if desired > 0 and ready == desired and avail == "True":
        st.ok("alloy-gateway deploy Ready", f"ready={ready}/{desired} Available=True")
    else:
        st.fail("alloy-gateway deploy Ready", f"ready={ready}/{desired} Available={avail or '?'}")

    # Aggregate nginx-auth logs across ALL gateway pods, only since the script
    # started, so we never count stale log lines from a prior incident window.
    since = (datetime.now(UTC) - timedelta(minutes=5)).strftime("%Y-%m-%dT%H:%M:%SZ")
    pods = _k(
        ctx,
        "-n",
        "alloy-gateway",
        "get",
        "pods",
        "-l",
        "app.kubernetes.io/name=alloy",
        "-o",
        "jsonpath={.items[*].metadata.name}",
    ).stdout
    gw_204 = 0
    for pod in pods.split():
        logs = _k(
            ctx,
            "-n",
            "alloy-gateway",
            "logs",
            pod,
            "-c",
            "nginx-auth",
            f"--since-time={since}",
            "--tail=1000",
        ).stdout
        gw_204 += sum(1 for line in logs.splitlines() if re.search(r"POST .* 204", line))
    if gw_204 > 0:
        st.ok("nginx-auth recent 204s", f"count={gw_204} across {desired} pod(s)")
    else:
        st.fail("nginx-auth recent 204s", f"no 204s since {since} across {desired} pod(s)")


def _stage5(st: _stage.Stages, ctx: str, inst: str) -> None:
    """Mimir ingestion + canary query."""
    st.section("Stage 5: Mimir distributor + ingester + canary query")
    pf = _port_forward(ctx, "mimir", "svc/mimir-distributor", 8080)
    if pf is None:
        st.fail("mimir distributor received_samples", "port-forward to mimir-distributor failed")
    else:
        proc, port = pf
        try:
            metrics = _http_text(f"http://localhost:{port}/metrics", timeout=5)
            rx = _metric_sum(metrics, "cortex_distributor_received_samples_total")
            if rx > 0:
                st.ok("mimir distributor received_samples", f"total={rx}")
            else:
                st.fail("mimir distributor received_samples", f"sum={rx}")
        finally:
            _pf_stop(proc)

    pf = _port_forward(ctx, "mimir", "svc/mimir-query-frontend", 8080)
    if pf is None:
        st.fail(f"mimir query master={inst}.cd", "port-forward to mimir-query-frontend failed")
        st.fail(
            "mimir query hetzner_api_rate_limit_remaining",
            "port-forward to mimir-query-frontend failed",
        )
        return
    proc, port = pf
    try:
        qurl = f"http://localhost:{port}/prometheus/api/v1/query"
        res = _http_text(
            qurl + "?" + urllib.parse.urlencode({"query": f'count({{master="{inst}.cd"}})'}),
            timeout=8,
        )
        count = _jq_str(res, "data", "result", 0, "value", 1)
        count_ok = False
        if count:
            try:
                count_ok = int(count) > 0
            except ValueError:
                count_ok = False
        if count_ok:
            st.ok(f"mimir query master={inst}.cd", f"series={count}")
        else:
            st.fail(f"mimir query master={inst}.cd", f"raw={res[:160]}")

        res = _http_text(
            qurl
            + "?"
            + urllib.parse.urlencode(
                {"query": f'hetzner_api_rate_limit_remaining{{master="{inst}.cd"}}'}
            ),
            timeout=8,
        )
        rate = _jq_str(res, "data", "result", 0, "value", 1)
        if rate:
            st.ok("mimir query hetzner_api_rate_limit_remaining", f"value={rate}")
        else:
            st.fail("mimir query hetzner_api_rate_limit_remaining", "no value returned")
    finally:
        _pf_stop(proc)


def _stage6(
    st: _stage.Stages, ctx: str, inst: str, bearer: str, now_ns: str, probe_label: str
) -> None:
    """Loki ingestion + ingester ring + canary query."""
    st.section("Stage 6: Loki distributor + ingester ring + canary query")
    sts = _k(
        ctx, "-n", "loki", "get", "sts", "-l", "app.kubernetes.io/component=ingester", "-o", "json"
    )
    sts_data = _json_loads(sts.stdout)
    expected = 0
    if isinstance(sts_data, dict):
        for item in sts_data.get("items") or []:
            if isinstance(item, dict):
                expected += (item.get("spec") or {}).get("replicas") or 0

    pf = _port_forward(ctx, "loki", "svc/loki-distributor", 3100)
    if pf is None:
        st.fail("loki distributor lines_received", "port-forward to loki-distributor failed")
        st.fail("loki ingester ring", "port-forward to loki-distributor failed")
    else:
        proc, port = pf
        try:
            metrics = _http_text(f"http://localhost:{port}/metrics", timeout=5)
            lines_rx = _metric_sum(metrics, "loki_distributor_lines_received_total")
            if lines_rx > 0:
                st.ok("loki distributor lines_received", f"total={lines_rx}")
            else:
                st.fail("loki distributor lines_received", f"sum={lines_rx}")

            ring_raw = _http_text(f"http://localhost:{port}/ring?format=json", timeout=5)
            ring = _json_loads(ring_raw)
            states: list[str] = []
            if isinstance(ring, dict):
                for shard in ring.get("shards") or []:
                    if isinstance(shard, dict):
                        state = shard.get("state")
                        if state not in (None, False):
                            states.append(str(state))
            if states:
                active = sum(1 for s in states if s == "ACTIVE")
                unhealthy = sum(1 for s in states if s == "UNHEALTHY")
            else:
                # Fallback for older /ring HTML endpoint.
                ring_html = _http_text(f"http://localhost:{port}/ring", timeout=5)
                active = ring_html.count("ACTIVE")
                unhealthy = ring_html.count("UNHEALTHY")
            if expected > 0 and active == expected and unhealthy == 0:
                st.ok("loki ingester ring", f"ACTIVE={active}/{expected} UNHEALTHY={unhealthy}")
            else:
                st.fail(
                    "loki ingester ring",
                    f"ACTIVE={active}/{expected} UNHEALTHY={unhealthy} "
                    f"(expected ACTIVE={expected}, UNHEALTHY=0)",
                )
        finally:
            _pf_stop(proc)

    pf = _port_forward(ctx, "loki", "svc/loki-query-frontend", 3100)
    if pf is None:
        st.fail("loki probe round-trip", "port-forward to loki-query-frontend failed")
        st.fail(f"loki query master={inst}.cd (24h)", "port-forward to loki-query-frontend failed")
        return
    proc, port = pf
    try:
        qurl = f"http://localhost:{port}/loki/api/v1/query_range"
        # Probe round-trip: query for the bearer-pushed probe from Stage 3.
        # This is a deterministic end-to-end check (gateway -> distributor ->
        # ingester -> query-frontend) that does not depend on master activity.
        if bearer:
            time.sleep(2)  # allow ingester to index
            probe_start = int(now_ns) - 60_000_000_000
            probe_end = int(now_ns) + 60_000_000_000
            res = _http_text(
                qurl
                + "?"
                + urllib.parse.urlencode(
                    {
                        "query": f'{{probe="{probe_label}"}}',
                        "start": str(probe_start),
                        "end": str(probe_end),
                        "limit": "1",
                        "direction": "BACKWARD",
                    }
                ),
                timeout=12,
            )
            probe_line = _jq_str(res, "data", "result", 0, "values", 0, 1)
            if probe_line:
                st.ok("loki probe round-trip", f"probe={probe_label} returned")
            else:
                st.fail("loki probe round-trip", f"no streams for probe={probe_label}")
        else:
            st.skip("loki probe round-trip", "no bearer fetched")

        # Master-side coverage: any log line from this master in the last 24h.
        # The pipeline is already validated by the probe round-trip above; this
        # check only confirms that the master-side Alloy systemd unit has
        # reached Loki at some point in the recent past.
        start = f"{int(time.time()) - 86400}000000000"
        end = f"{int(time.time())}000000000"
        res = _http_text(
            qurl
            + "?"
            + urllib.parse.urlencode(
                {
                    "query": f'{{master="{inst}.cd"}}',
                    "start": start,
                    "end": end,
                    "limit": "1",
                    "direction": "BACKWARD",
                }
            ),
            timeout=12,
        )
        line = _jq_str(res, "data", "result", 0, "values", 0, 1)
        last_ts = _jq_str(res, "data", "result", 0, "values", 0, 0)
        if line:
            try:
                ts_int = int(last_ts[:10])
            except ValueError:
                ts_int = 0
            age = int(time.time()) - ts_int
            preview = line[:80]
            st.ok(f"loki query master={inst}.cd (24h)", f"age={age}s: {preview}…")
        else:
            st.fail(f"loki query master={inst}.cd (24h)", "no streams in last 24h")
    finally:
        _pf_stop(proc)


def _grafana_auth_headers(ctx: str) -> dict[str, str] | None:
    """Prefer $GRAFANA_TOKEN; else the in-cluster `grafana` admin secret; else None."""
    token = os.environ.get("GRAFANA_TOKEN") or ""
    if token:
        return {"Authorization": f"Bearer {token}"}

    def secret_field(field: str) -> str:
        res = _k(
            ctx, "-n", "grafana", "get", "secret", "grafana", "-o", f"jsonpath={{.data.{field}}}"
        )
        if res.returncode != 0:
            return ""
        try:
            return base64.b64decode(res.stdout).decode("utf-8", errors="replace")
        except ValueError:
            return ""

    g_pass = secret_field("admin-password")
    if not g_pass:
        return None
    g_user = secret_field("admin-user") or "admin"
    basic = base64.b64encode(f"{g_user}:{g_pass}".encode()).decode()
    return {"Authorization": f"Basic {basic}"}


def _stage7(st: _stage.Stages, ctx: str) -> None:
    """Grafana frontend + datasource proxy."""
    st.section("Stage 7: Grafana")
    health = _http_status("https://grafana.cd.percona.com/api/health", timeout=5)
    if health == "200":
        st.ok("grafana.cd.percona.com /api/health", "HTTP=200")
    else:
        st.fail("grafana.cd.percona.com /api/health", f"HTTP={health}")

    pods = _k(
        ctx, "-n", "grafana", "get", "pods", "-l", "app.kubernetes.io/name=grafana", "-o", "json"
    )
    pods_data = _json_loads(pods.stdout)
    g_ready = 0
    if isinstance(pods_data, dict):
        for item in pods_data.get("items") or []:
            if not isinstance(item, dict):
                continue
            status = item.get("status") or {}
            if status.get("phase") != "Running":
                continue
            flags = [c.get("ready") for c in status.get("containerStatuses") or []]
            if all(flags):
                g_ready += 1
    if g_ready >= 1:
        st.ok("grafana pods Running", f"ready={g_ready}")
    else:
        st.fail("grafana pods Running", f"ready={g_ready}")

    # Datasource proxy + UID assertions, auth-optional. If no credentials are
    # available, SKIP cleanly (incident operators without Grafana credentials
    # should still get a green run from the other stages).
    auth = _grafana_auth_headers(ctx)
    if auth is None:
        reason = "no GRAFANA_TOKEN and no admin secret readable"
        st.skip("grafana datasource uid=mimir", reason)
        st.skip("grafana datasource uid=loki", reason)
        st.skip("grafana datasource uid=tempo", reason)
        st.skip("grafana mimir proxy query", reason)
        return

    # Probe whether the credentials actually authenticate (Grafana behind
    # Authentik OIDC will reject local admin basic-auth with 401 even when the
    # secret exists in-cluster). If auth doesn't work, SKIP cleanly rather
    # than FAIL the operator.
    probe_code = _http_status("https://grafana.cd.percona.com/api/user", headers=auth, timeout=8)
    if probe_code != "200":
        reason = f"auth probe /api/user HTTP={probe_code} (Grafana behind OIDC?)"
        st.skip("grafana datasource uid=mimir", reason)
        st.skip("grafana datasource uid=loki", reason)
        st.skip("grafana datasource uid=tempo", reason)
        st.skip("grafana mimir proxy query", reason)
        return

    ds_body = (
        _http_text("https://grafana.cd.percona.com/api/datasources", headers=auth, timeout=8)
        or "[]"
    )
    ds_data = _json_loads(ds_body)
    ds_list = ds_data if isinstance(ds_data, list) else []
    for uid in ("mimir", "loki", "tempo"):
        if any(isinstance(d, dict) and d.get("uid") == uid for d in ds_list):
            st.ok(f"grafana datasource uid={uid}", "present")
        else:
            st.fail(f"grafana datasource uid={uid}", "uid not found in /api/datasources")

    pq = _http_text(
        "https://grafana.cd.percona.com/api/datasources/proxy/uid/mimir/api/v1/query?"
        + urllib.parse.urlencode({"query": "vector(1)"}),
        headers=auth,
        timeout=8,
    )
    pqv = _jq_str(pq, "data", "result", 0, "value", 1)
    if pqv == "1":
        st.ok("grafana mimir proxy query", "vector(1)=1")
    else:
        st.fail("grafana mimir proxy query", f"raw={pq[:160]}")


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(
        prog="cdpctl verify-observability",
        description="LGTM push pipeline health check",
        epilog=(
            "Environment:\n"
            "  KUBE_CONTEXT     kubectl context (default: percona-ci-platform)\n"
            "  AWS_PROFILE      AWS profile for the bearer fetch (unset: bearer stages SKIP)\n"
            "  GRAFANA_TOKEN    optional Grafana API token for datasource + proxy probes"
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    ap.add_argument(
        "inst",
        nargs="?",
        default="ps3",
        metavar="<inst>",
        help="master shortname (default: ps3); resolves to <inst>.cd.percona.com",
    )
    ap.add_argument(
        "--skip-master", action="store_true", help="skip SSH-dependent master-side stages"
    )
    _stage.add_output_flags(ap)
    args = ap.parse_args(argv)

    inst: str = args.inst
    ctx = os.environ.get("KUBE_CONTEXT") or "percona-ci-platform"
    profile = os.environ.get("AWS_PROFILE", "")
    host = f"{inst}.cd.percona.com"
    ssh_key = os.path.expanduser("~/.ssh/id_rsa")

    tools = ["aws", "kubectl", "dig"]
    if not args.skip_master:
        tools.append("ssh")
    _stage.require(*tools)

    mode = _stage.output_mode(args)
    st = _stage.Stages(quiet=(mode != "human"))
    st.echo(f"verify-observability  master={inst}  cluster-context={ctx}")

    cluster_ok, bearer = _stage0(st, ctx, profile)
    if not cluster_ok:
        return _finalize(st, inst, ctx, mode, 2)
    _stage_master(st, inst, host, ssh_key, args.skip_master)
    now_ns, probe_label = _stage3(st, inst, bearer)
    _stage4(st, ctx)
    _stage5(st, ctx, inst)
    _stage6(st, ctx, inst, bearer, now_ns, probe_label)
    _stage7(st, ctx)
    return _finalize(st, inst, ctx, mode, st.exit_code())


if __name__ == "__main__":
    sys.exit(main())
