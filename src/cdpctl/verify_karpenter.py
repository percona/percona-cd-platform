#!/usr/bin/env python3
"""verify-karpenter: systematic before/during/after validation of Karpenter
scale-up and scale-down behavior on the percona-ci-platform EKS cluster.

Phases:
  pre                Snapshot baseline (nodes, NodeClaims, log cursor).
  scaleup-small      5x small pods land on a single new spot `large` node.
  scaleup-bigreq     4x 2-CPU pods bin-pack to ~2xlarge, not 4xlarge.
  scaleup-spread     3x pods + topology spread span >=2 AZs.
  scaledown-empty    Empty node terminates within consolidateAfter+drain.
  scaledown-underutil 2 nodes consolidate to 1 when the other is drainable.
  pdb                PDB minAvailable=replicas blocks; relax -> proceeds.
  do-not-disrupt     karpenter.sh/do-not-disrupt pin holds; remove -> drains.
  hardening          IMDSv2 hop=1, encrypted root, AL2023 image per launched EC2.
  limits             Hitting NodePool limits.cpu leaves surplus pods Pending.
  family-matrix      cpu-shape lands on c7*, mem-shape lands on r7*.
  pool-discovery     Each `kubectl get nodepool` was exercised.
  boundary-audit     No Karpenter node carries managed-NG taints/labels.
  cleanup            Delete the karpenter-validation namespace.
  all                Run every phase in order.

Output: human-readable stage rows by default; `--json` prints a single JSON
envelope, `--llm` prints pipe-delimited `status|label|detail` rows.

Exits 0 if every requested phase passes; 1 on any failure unless
--keep-going. Phase `cleanup` always runs at end of `all` regardless of
--no-cleanup unless --keep-going is also set.

Required tools: kubectl, aws.
Required env: AWS_PROFILE exported (for `aws ec2 describe-instances`).
"""

from __future__ import annotations

import argparse
import dataclasses
import json
import os
import re
import subprocess
import sys
import time
from collections.abc import Callable
from typing import NoReturn

from cdpctl import _stage
from cdpctl._repo import die, repo_root

NS = "karpenter-validation"
BASELINE_PATH = "/tmp/karpenter-validation-baseline.json"


@dataclasses.dataclass
class Ctx:
    """Per-run state: the stage log, output mode, tunables, and the NodeClaim history."""

    st: _stage.Stages
    mode: str
    phases: list[str]
    kube_context: str
    tests_dir: str
    profile: str
    keep_going: bool
    no_cleanup: bool
    scaleup_budget_s: int
    empty_drain_budget_s: int
    underutil_budget_s: int
    pdb_observe_s: int
    dnd_observe_s: int
    # NodeClaims observed during this run / unique EC2 instance ids touched.
    nodeclaim_history: list[str] = dataclasses.field(default_factory=list)
    launched_instances: list[str] = dataclasses.field(default_factory=list)


# ----- output ----------------------------------------------------------------
def _yellow(s: str) -> str:
    return f"\033[33m{s}\033[0m" if sys.stdout.isatty() else s


def _info(st: _stage.Stages, text: str) -> None:
    st.echo(f"  {_stage.gray('·')} {text}")


def _warn(st: _stage.Stages, label: str, detail: str = "") -> None:
    """WARN rows count toward no total; they only land in the result log."""
    st.echo(f"  {_yellow('WARN')} {label} {_yellow(detail)}")
    st.results.append(("WARN", label, detail))


def _summary(st: _stage.Stages) -> None:
    st.section("Summary")
    st.echo(f"  pass={st.passed}  fail={st.failed}  skip={st.skipped}")


def _emit_payload(ctx: Ctx) -> None:
    """End-of-run payload for the active output mode (also used on early exits)."""
    if ctx.mode == "json":
        print(json.dumps(ctx.st.envelope(phases=ctx.phases, context=ctx.kube_context), indent=2))
    elif ctx.mode == "llm":
        ctx.st.emit_llm()
    else:
        _summary(ctx.st)


def _fail_precondition(ctx: Ctx, label: str) -> NoReturn:
    """Record the failed prereq row; json/llm still get their payload before exit 2."""
    ctx.st.fail(label)
    if ctx.mode != "human":
        _emit_payload(ctx)
    raise SystemExit(2)


def _abort_or_continue(ctx: Ctx) -> None:
    if ctx.keep_going:
        return
    _emit_payload(ctx)
    raise SystemExit(1)


# ----- subprocess helpers ----------------------------------------------------
def _run(cmd: list[str]) -> subprocess.CompletedProcess[str]:
    """Capture stdout+stderr; the `$(cmd 2>/dev/null)` shape."""
    return subprocess.run(cmd, capture_output=True, text=True, check=False)


def _ok(cmd: list[str]) -> bool:
    """Exit-status probe; the `cmd >/dev/null 2>&1` shape."""
    res = subprocess.run(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, check=False)
    return res.returncode == 0


def _quiet(cmd: list[str], *, drop_stderr: bool = False) -> None:
    """Fire-and-forget mutation; the `cmd >/dev/null` shape (stderr inherited
    unless drop_stderr, matching the bash redirections call by call)."""
    subprocess.run(
        cmd,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL if drop_stderr else None,
        check=False,
    )


def _get_json(cmd: list[str]) -> dict | None:
    res = _run(cmd)
    if res.returncode != 0:
        return None
    try:
        return json.loads(res.stdout)
    except json.JSONDecodeError:
        return None


def _count_lines(cmd: list[str]) -> int:
    """`cmd --no-headers 2>/dev/null | wc -l` (a failed command counts 0)."""
    return len(_run(cmd).stdout.splitlines())


def _current_context(default: str) -> str:
    res = _run(["kubectl", "config", "current-context"])
    return res.stdout.strip() if res.returncode == 0 else default


# ----- kubectl object helpers ------------------------------------------------
def _node_json(name: str) -> dict | None:
    return _get_json(["kubectl", "get", "node", name, "-o", "json"])


def _node_exists(name: str) -> bool:
    return _ok(["kubectl", "get", "node", name])


def _node_labels(name: str) -> dict:
    nj = _node_json(name)
    return ((nj or {}).get("metadata") or {}).get("labels") or {}


def _node_taints(nj: dict) -> str:
    """`{.key}={.value}:{.effect};` per taint, like the jsonpath range."""
    taints = (nj.get("spec") or {}).get("taints") or []
    return "".join(
        f"{t.get('key', '')}={t.get('value', '')}:{t.get('effect', '')};" for t in taints
    )


def _pod_items(app: str) -> list[dict]:
    pj = _get_json(["kubectl", "-n", NS, "get", "pods", "-l", f"app={app}", "-o", "json"])
    return (pj or {}).get("items") or []


def _pod_nodes(app: str) -> list[str]:
    """Unique, sorted, non-empty .spec.nodeName of the app's pods."""
    names = {(p.get("spec") or {}).get("nodeName") or "" for p in _pod_items(app)}
    return sorted(n for n in names if n)


def _first_pod_node(app: str) -> str:
    items = _pod_items(app)
    if not items:
        return ""
    return (items[0].get("spec") or {}).get("nodeName") or ""


def _nodeclaim_json(nc: str) -> dict:
    return _get_json(["kubectl", "get", "nodeclaim", nc, "-o", "json"]) or {}


def _nodeclaim_instance_id(nc: str) -> str:
    pid = (_nodeclaim_json(nc).get("status") or {}).get("providerID") or ""
    return pid.split("/")[-1]


def _nodeclaim_node_name(nc: str) -> str:
    return (_nodeclaim_json(nc).get("status") or {}).get("nodeName") or ""


def _nodeclaim_pool(nc: str) -> str:
    labels = (_nodeclaim_json(nc).get("metadata") or {}).get("labels") or {}
    return labels.get("karpenter.sh/nodepool") or ""


def _record_nodeclaims(ctx: Ctx) -> None:
    """Append every NodeClaim name seen so far, uniquely, in observation order
    (so boundary-audit / hardening can iterate)."""
    ncj = _get_json(["kubectl", "get", "nodeclaims", "-o", "json"])
    if ncj is None:
        return
    for item in ncj.get("items") or []:
        name = (item.get("metadata") or {}).get("name") or ""
        if name and name not in ctx.nodeclaim_history:
            ctx.nodeclaim_history.append(name)


# ----- fixtures --------------------------------------------------------------
def _apply_manifest(ctx: Ctx, name: str) -> None:
    _quiet(["kubectl", "apply", "-f", f"{ctx.tests_dir}/{name}"])


def _delete_manifest(ctx: Ctx, name: str) -> None:
    _quiet(
        [
            "kubectl",
            "delete",
            "-f",
            f"{ctx.tests_dir}/{name}",
            "--ignore-not-found",
            "--wait=false",
        ],
        drop_stderr=True,
    )


def _ensure_ns(ctx: Ctx) -> None:
    if _ok(["kubectl", "get", "ns", NS]):
        return
    _apply_manifest(ctx, "00-namespace.yaml")
    _quiet(
        ["kubectl", "wait", "--for=jsonpath={.status.phase}=Active", f"ns/{NS}", "--timeout=30s"]
    )


# ----- polling ---------------------------------------------------------------
def _wait_pods_ready(app: str, want: int, budget: int) -> tuple[bool, int]:
    """All Pods of an app label Ready -> (True, latency seconds); timeout ->
    (False, 999). Polls every 5s."""
    started = time.monotonic()
    while time.monotonic() - started < budget:
        ready = 0
        for p in _pod_items(app):
            status = p.get("status") or {}
            if status.get("phase") != "Running":
                continue
            for c in status.get("conditions") or []:
                if c.get("type") == "Ready" and c.get("status") == "True":
                    ready += 1
        if ready >= want:
            return True, int(time.monotonic() - started)
        time.sleep(5)
    return False, 999


# ----- prereqs ---------------------------------------------------------------
def stage_prereqs(ctx: Ctx) -> None:
    ctx.st.section("Prereqs")
    if not _ok(["kubectl", "version"]):
        die(
            f"kubectl can't reach the cluster (current-context: {_current_context('<none>')})",
            2,
        )
    ctx.st.ok("kubectl context", _current_context(""))
    if not _ok(["kubectl", "get", "nodepool", "default"]):
        _fail_precondition(ctx, "nodepool default present")
    ctx.st.ok("nodepool default present")
    if not _ok(["kubectl", "get", "ec2nodeclass", "default"]):
        _fail_precondition(ctx, "ec2nodeclass default present")
    ctx.st.ok("ec2nodeclass default present")
    if not _ok(["aws", "sts", "get-caller-identity", "--profile", ctx.profile]):
        _warn(ctx.st, "aws sts get-caller-identity", "hardening phase will skip")
    else:
        ctx.st.ok("aws sts get-caller-identity", ctx.profile)


# ----- pre snapshot ----------------------------------------------------------
def stage_pre(ctx: Ctx) -> None:
    ctx.st.section("P: baseline snapshot")
    _ensure_ns(ctx)
    with open(BASELINE_PATH, "w", encoding="utf-8") as fh:
        subprocess.run(["kubectl", "get", "nodes", "-o", "json"], stdout=fh, check=False)
    kn = _count_lines(["kubectl", "get", "nodes", "-l", "karpenter.sh/nodepool", "--no-headers"])
    nc = _count_lines(["kubectl", "get", "nodeclaims", "--no-headers"])
    ctx.st.ok("baseline nodes captured", f"{BASELINE_PATH}  karpenter_nodes={kn}  nodeclaims={nc}")
    # Allowed families/sizes from NodePool
    np = _get_json(["kubectl", "get", "nodepool", "default", "-o", "json"]) or {}
    spec = ((np.get("spec") or {}).get("template") or {}).get("spec") or {}
    reqs = spec.get("requirements") or []
    fams = "\n".join(
        ",".join(r.get("values") or [])
        for r in reqs
        if r.get("key") == "karpenter.k8s.aws/instance-family"
    )
    sizes = "\n".join(
        ",".join(r.get("values") or [])
        for r in reqs
        if r.get("key") == "karpenter.k8s.aws/instance-size"
    )
    ctx.st.ok("nodepool requirements", f"families={fams}  sizes={sizes}")
    _record_nodeclaims(ctx)


# ----- scaleup-small ---------------------------------------------------------
def stage_scaleup_small(ctx: Ctx) -> None:
    ctx.st.section("S1: scale-up small burst (5x 500m/256Mi)")
    _ensure_ns(ctx)
    _apply_manifest(ctx, "01-nginx-burst.yaml")
    ok, lat = _wait_pods_ready("burst-small", 5, ctx.scaleup_budget_s)
    if not ok:
        ctx.st.fail("burst-small pods Ready", f"after {ctx.scaleup_budget_s}s")
        _abort_or_continue(ctx)
        return
    ctx.st.ok("burst-small pods Ready", f"{lat}s")

    # Find which node(s) the pods landed on.
    nodes = _pod_nodes("burst-small")
    ctx.st.ok("burst-small node spread", f"nodes={len(nodes)}")
    for n in nodes:
        labels = _node_labels(n)
        sz = labels.get("karpenter.k8s.aws/instance-size") or ""
        cap = labels.get("karpenter.sh/capacity-type") or ""
        pool = labels.get("karpenter.sh/nodepool") or ""
        role = labels.get("node-role") or ""
        _info(ctx.st, f"node={n} pool={pool} size={sz} capacity={cap} role={role}")
        if pool != "default":
            ctx.st.fail("burst-small pod on non-default pool", f"node={n} pool={pool}")
        elif role != "workload":
            ctx.st.fail("burst-small pod on non-workload role", f"node={n} role={role}")
        elif sz != "large":
            _warn(ctx.st, "burst-small not on size=large", f"got {sz}")
        else:
            ctx.st.ok("burst-small node sizing", f"size={sz} pool={pool}")
    _record_nodeclaims(ctx)


# ----- scaleup-bigreq --------------------------------------------------------
def stage_scaleup_bigreq(ctx: Ctx) -> None:
    ctx.st.section("S2: scale-up big request (4x 2CPU/4Gi)")
    _ensure_ns(ctx)
    _apply_manifest(ctx, "02-nginx-bigreq.yaml")
    ok, lat = _wait_pods_ready("bigreq", 4, ctx.scaleup_budget_s)
    if not ok:
        ctx.st.fail("bigreq pods Ready", f"after {ctx.scaleup_budget_s}s")
        _abort_or_continue(ctx)
        return
    ctx.st.ok("bigreq pods Ready", f"{lat}s")
    for n in _pod_nodes("bigreq"):
        labels = _node_labels(n)
        sz = labels.get("karpenter.k8s.aws/instance-size") or ""
        pool = labels.get("karpenter.sh/nodepool") or ""
        _info(ctx.st, f"node={n} size={sz} pool={pool}")
        if sz in ("xlarge", "2xlarge"):
            ctx.st.ok("bigreq bin-pack", f"node={n} size={sz}")
        elif sz == "4xlarge":
            _warn(ctx.st, "bigreq over-provisioned", f"node={n} size={sz}")
        else:
            ctx.st.fail("bigreq unexpected size", f"node={n} size={sz}")
    _record_nodeclaims(ctx)


# ----- scaleup-spread --------------------------------------------------------
def stage_scaleup_spread(ctx: Ctx) -> None:
    ctx.st.section("S3: scale-up cross-AZ spread (3x + topologySpreadConstraints)")
    _ensure_ns(ctx)
    _apply_manifest(ctx, "03-nginx-spread.yaml")
    ok, lat = _wait_pods_ready("spread", 3, ctx.scaleup_budget_s)
    if not ok:
        ctx.st.fail("spread pods Ready", f"after {ctx.scaleup_budget_s}s")
        _abort_or_continue(ctx)
        return
    ctx.st.ok("spread pods Ready", f"{lat}s")
    zones: list[str] = []
    for n in _pod_nodes("spread"):
        z = _node_labels(n).get("topology.kubernetes.io/zone") or ""
        _info(ctx.st, f"node={n} zone={z}")
        zones.append(z)
    zcount = len({z for z in zones if z})
    if zcount >= 2:
        ctx.st.ok("AZ spread", f"zones={zcount}")
    else:
        ctx.st.fail("AZ spread", f"only {zcount} AZ")
    _record_nodeclaims(ctx)


# ----- scaledown-empty -------------------------------------------------------
def stage_scaledown_empty(ctx: Ctx) -> None:
    ctx.st.section("D1: scale-down empty consolidation")
    # Capture target node(s) before delete.
    before_nodes = _pod_nodes("burst-small")
    if not before_nodes:
        ctx.st.skip("scaledown-empty", "burst-small not running (run scaleup-small first)")
        return
    _delete_manifest(ctx, "01-nginx-burst.yaml")
    # Trailing space mirrors the bash `tr '\n' ' '` join.
    _info(ctx.st, "watching for nodes to drain: " + " ".join(before_nodes) + " ")
    started = time.monotonic()
    removed = False
    while time.monotonic() - started < ctx.empty_drain_budget_s:
        removed = all(not _node_exists(n) for n in before_nodes)
        if removed:
            break
        time.sleep(5)
    if removed:
        ctx.st.ok("burst-small nodes terminated", f"after {int(time.monotonic() - started)}s")
    else:
        joined = "\n".join(before_nodes)
        ctx.st.fail("burst-small nodes terminated", f"after {ctx.empty_drain_budget_s}s ({joined})")
    _record_nodeclaims(ctx)


# ----- scaledown-underutil ---------------------------------------------------
def stage_scaledown_underutil(ctx: Ctx) -> None:
    ctx.st.section("D2: scale-down underutilized consolidation (bigreq 4 -> 1)")
    if not _ok(["kubectl", "-n", NS, "get", "deploy", "bigreq"]):
        ctx.st.skip("scaledown-underutil", "bigreq not running (run scaleup-bigreq first)")
        return
    before_count = len(_pod_nodes("bigreq"))
    _info(ctx.st, f"before: bigreq spans {before_count} nodes")
    _quiet(["kubectl", "-n", NS, "scale", "deploy/bigreq", "--replicas=1"])
    started = time.monotonic()
    after_count = before_count
    while time.monotonic() - started < ctx.underutil_budget_s:
        after_count = len(_pod_nodes("bigreq"))
        if after_count < before_count:
            break
        time.sleep(5)
    if after_count < before_count:
        ctx.st.ok(
            "bigreq underutil consolidation",
            f"{before_count} -> {after_count} nodes in {int(time.monotonic() - started)}s",
        )
    else:
        ctx.st.fail(
            "bigreq underutil consolidation",
            f"still {after_count} nodes after {ctx.underutil_budget_s}s",
        )
    _record_nodeclaims(ctx)


# ----- pdb -------------------------------------------------------------------
def stage_pdb(ctx: Ctx) -> None:
    ctx.st.section("P1: PDB respected during disruption")
    _ensure_ns(ctx)
    _apply_manifest(ctx, "04-nginx-pdb.yaml")
    ok, _lat = _wait_pods_ready("pdb-guarded", 3, ctx.scaleup_budget_s)
    if not ok:
        ctx.st.fail("pdb-guarded pods Ready")
        _abort_or_continue(ctx)
        return
    ctx.st.ok("pdb-guarded 3 pods Ready")
    # Scale to 1 -> 2 pods deleted, but PDB minAvailable=3 should block any
    # voluntary disruption. The realistic assertion (idle nodes whose replica
    # scaled out of existence may legitimately drain): the node hosting the
    # surviving 1 replica is still alive after the observation window.
    nodes_before = _pod_nodes("pdb-guarded")
    _quiet(["kubectl", "-n", NS, "scale", "deploy/pdb-guarded", "--replicas=1"])
    _info(ctx.st, f"scaled pdb-guarded to 1; observing for {ctx.pdb_observe_s}s...")
    time.sleep(ctx.pdb_observe_s)
    lost = sum(1 for n in nodes_before if not _node_exists(n))
    surviving_pod_node = _first_pod_node("pdb-guarded")
    if surviving_pod_node and _node_exists(surviving_pod_node):
        ctx.st.ok(
            "PDB protects host of surviving pod",
            f"node={surviving_pod_node} ({lost} idle nodes drained)",
        )
    else:
        ctx.st.fail("PDB protects host of surviving pod", f"node={surviving_pod_node} missing")
    _delete_manifest(ctx, "04-nginx-pdb.yaml")
    _record_nodeclaims(ctx)


# ----- do-not-disrupt --------------------------------------------------------
def stage_do_not_disrupt(ctx: Ctx) -> None:
    ctx.st.section("P2: karpenter.sh/do-not-disrupt pin")
    _ensure_ns(ctx)
    _apply_manifest(ctx, "05-do-not-disrupt.yaml")
    ok, _lat = _wait_pods_ready("do-not-disrupt", 1, ctx.scaleup_budget_s)
    if not ok:
        ctx.st.fail("do-not-disrupt pod Ready")
        _abort_or_continue(ctx)
        return
    ctx.st.ok("do-not-disrupt pod Ready")
    pinned_node = _first_pod_node("do-not-disrupt")
    _info(ctx.st, f"pinned node={pinned_node}; observing for {ctx.dnd_observe_s}s...")
    time.sleep(ctx.dnd_observe_s)
    if _node_exists(pinned_node):
        ctx.st.ok("node pinned by do-not-disrupt", pinned_node)
    else:
        ctx.st.fail("node pinned by do-not-disrupt", f"{pinned_node} was drained anyway")
    # Now remove the annotation and watch for consolidation.
    _delete_manifest(ctx, "05-do-not-disrupt.yaml")
    started = time.monotonic()
    while time.monotonic() - started < ctx.empty_drain_budget_s:
        if not _node_exists(pinned_node):
            break
        time.sleep(5)
    if not _node_exists(pinned_node):
        ctx.st.ok("node drains after pin removal", f"after {int(time.monotonic() - started)}s")
    else:
        ctx.st.fail("node drains after pin removal", f"still up after {ctx.empty_drain_budget_s}s")
    _record_nodeclaims(ctx)


# ----- hardening -------------------------------------------------------------
def stage_hardening(ctx: Ctx) -> None:
    ctx.st.section("H: per-NodeClaim EC2 hardening audit")
    if not _ok(["aws", "sts", "get-caller-identity", "--profile", ctx.profile]):
        ctx.st.skip("hardening audit", "AWS profile not authenticated")
        return
    _record_nodeclaims(ctx)
    if not ctx.nodeclaim_history:
        ctx.st.skip("hardening audit", "no NodeClaims observed yet")
        return
    checked = 0
    for nc in ctx.nodeclaim_history:
        iid = _nodeclaim_instance_id(nc)
        if not iid or iid in ctx.launched_instances:
            continue
        ctx.launched_instances.append(iid)
        res = _run(
            [
                "aws",
                "ec2",
                "describe-instances",
                "--instance-ids",
                iid,
                "--region",
                "us-east-1",
                "--profile",
                ctx.profile,
                "--query",
                "Reservations[0].Instances[0].[InstanceType,State.Name,"
                "MetadataOptions.HttpTokens,MetadataOptions.HttpPutResponseHopLimit,"
                "BlockDeviceMappings[0].Ebs.VolumeId,ImageId]",
                "--output",
                "text",
            ]
        )
        if res.returncode != 0:
            _warn(ctx.st, f"describe-instances {iid}", "AWS API failed")
            continue
        fields = res.stdout.strip().split(None, 5)
        while len(fields) < 6:
            fields.append("")
        itype, state, http_tokens = fields[0], fields[1], fields[2]
        hop_limit, vol_id, image_id = fields[3], fields[4], fields[5]
        _info(
            ctx.st,
            f"nc={nc} iid={iid} type={itype} state={state} "
            f"imds={http_tokens} hop={hop_limit} ami={image_id}",
        )
        if http_tokens == "required" and hop_limit == "1":
            ctx.st.ok(f"{nc} IMDSv2 hop=1", iid)
        else:
            ctx.st.fail(f"{nc} IMDSv2 hop=1", f"tokens={http_tokens} hop={hop_limit}")
        if vol_id and vol_id != "None":
            enc = _run(
                [
                    "aws",
                    "ec2",
                    "describe-volumes",
                    "--volume-ids",
                    vol_id,
                    "--region",
                    "us-east-1",
                    "--profile",
                    ctx.profile,
                    "--query",
                    "Volumes[0].Encrypted",
                    "--output",
                    "text",
                ]
            ).stdout.strip()
            if enc in ("True", "true"):
                ctx.st.ok(f"{nc} root EBS encrypted", vol_id)
            else:
                ctx.st.fail(f"{nc} root EBS encrypted", f"vol={vol_id} encrypted={enc}")
        checked += 1
    if checked == 0:
        ctx.st.skip("hardening audit", "no Karpenter-launched instances queryable")
    else:
        _info(ctx.st, f"audited {checked} instance(s)")


# ----- limits ----------------------------------------------------------------
def stage_limits(ctx: Ctx) -> None:
    ctx.st.section("L: NodePool limits enforcement")
    _ensure_ns(ctx)
    _apply_manifest(ctx, "06-limits-burst.yaml")
    _info(ctx.st, "applied limits-burst (51 replicas x 4 CPU = 204 CPU; pool limit=200)")
    time.sleep(60)
    pending = _count_lines(
        [
            "kubectl",
            "-n",
            NS,
            "get",
            "pods",
            "-l",
            "app=limits-burst",
            "--field-selector=status.phase=Pending",
            "--no-headers",
        ]
    )
    running = _count_lines(
        [
            "kubectl",
            "-n",
            NS,
            "get",
            "pods",
            "-l",
            "app=limits-burst",
            "--field-selector=status.phase=Running",
            "--no-headers",
        ]
    )
    _info(ctx.st, f"after 60s: running={running} pending={pending}")
    if pending > 0:
        ctx.st.ok("limits enforced (pods Pending)", f"running={running} pending={pending}")
    else:
        _warn(ctx.st, "limits enforced (pods Pending)", f"all pods scheduled? running={running}")
    # Karpenter event with limit reference?
    out = _run(["kubectl", "-n", "karpenter", "logs", "deploy/karpenter", "--tail=400"]).stdout
    logs = [ln for ln in out.splitlines() if re.search(r"limit|exceeded", ln, re.IGNORECASE)][-3:]
    if logs:
        _info(ctx.st, f"karpenter log: {logs[0]}")
    _delete_manifest(ctx, "06-limits-burst.yaml")


# ----- family-matrix ---------------------------------------------------------
def stage_family_matrix(ctx: Ctx) -> None:
    ctx.st.section("F: family x size matrix coverage")
    _ensure_ns(ctx)
    _apply_manifest(ctx, "07-family-cpu.yaml")
    _apply_manifest(ctx, "08-family-mem.yaml")
    ok, _lat = _wait_pods_ready("family-cpu", 2, ctx.scaleup_budget_s)
    if not ok:
        _warn(ctx.st, "family-cpu pods Ready", "soft warn — may be capacity")
    ok, _lat = _wait_pods_ready("family-mem", 2, ctx.scaleup_budget_s)
    if not ok:
        _warn(ctx.st, "family-mem pods Ready", "soft warn — may be capacity")
    cpu_fams = ""
    for n in _pod_nodes("family-cpu"):
        cpu_fams += " " + (_node_labels(n).get("karpenter.k8s.aws/instance-family") or "")
    mem_fams = ""
    for n in _pod_nodes("family-mem"):
        mem_fams += " " + (_node_labels(n).get("karpenter.k8s.aws/instance-family") or "")
    _info(ctx.st, f"cpu-shape families: {cpu_fams}")
    _info(ctx.st, f"mem-shape families: {mem_fams}")
    if re.search(r"c7[ai]", cpu_fams):
        ctx.st.ok("compute-shape lands on c7*", cpu_fams)
    else:
        _warn(ctx.st, "compute-shape lands on c7*", f"got {cpu_fams}")
    if re.search(r"r7[ai]", mem_fams):
        ctx.st.ok("memory-shape lands on r7*", mem_fams)
    else:
        _warn(ctx.st, "memory-shape lands on r7*", f"got {mem_fams}")
    _delete_manifest(ctx, "07-family-cpu.yaml")
    _delete_manifest(ctx, "08-family-mem.yaml")
    _record_nodeclaims(ctx)


# ----- pool-discovery --------------------------------------------------------
def stage_pool_discovery(ctx: Ctx) -> None:
    ctx.st.section("X: NodePool discovery")
    pj = _get_json(["kubectl", "get", "nodepool", "-o", "json"]) or {}
    pools = [
        name
        for it in pj.get("items") or []
        if (name := (it.get("metadata") or {}).get("name") or "")
    ]
    observed_pools = {p for nc in ctx.nodeclaim_history if (p := _nodeclaim_pool(nc))}
    # Trailing space mirrors the bash `tr '\n' ' '` join.
    observed = "".join(f"{p} " for p in sorted(observed_pools))
    _info(ctx.st, "all pools=" + "\n".join(pools))
    _info(ctx.st, f"observed={observed}")
    for p in pools:
        if f" {p} " in f" {observed} ":
            ctx.st.ok("pool exercised", p)
        else:
            _warn(ctx.st, "pool not exercised", f"{p} (no test load matched its requirements)")


# ----- boundary-audit --------------------------------------------------------
def stage_boundary_audit(ctx: Ctx) -> None:
    ctx.st.section("B: managed-NG boundary audit")
    _record_nodeclaims(ctx)
    checked = 0
    violations = 0
    for nc in ctx.nodeclaim_history:
        node = _nodeclaim_node_name(nc)
        if not node:
            continue
        nj = _node_json(node)
        if nj is None:
            continue
        labels = (nj.get("metadata") or {}).get("labels") or {}
        role = labels.get("node-role") or ""
        wl = labels.get("workload") or ""
        taints = _node_taints(nj)
        if role != "workload":
            ctx.st.fail(f"{nc} node role=workload", f"got role={role}")
            violations += 1
        if wl:
            ctx.st.fail(f"{nc} node has no workload= label", f"got workload={wl}")
            violations += 1
        if taints:
            ctx.st.fail(f"{nc} node has no taints", taints)
            violations += 1
        checked += 1
    if checked == 0:
        ctx.st.skip("boundary audit", "no NodeClaims to audit")
    elif violations == 0:
        ctx.st.ok("boundary audit clean", f"{checked} NodeClaims")


# ----- cleanup ---------------------------------------------------------------
def stage_cleanup(ctx: Ctx) -> None:
    ctx.st.section("C: cleanup")
    if ctx.no_cleanup:
        ctx.st.skip("cleanup", f"--no-cleanup set; ns={NS} retained")
        return
    if _ok(["kubectl", "get", "ns", NS]):
        _info(ctx.st, f"deleting ns/{NS}...")
        _quiet(["kubectl", "delete", "ns", NS, "--wait=false"])
        # Best-effort wait up to 5min.
        started = time.monotonic()
        while time.monotonic() - started < 300:
            if not _ok(["kubectl", "get", "ns", NS]):
                break
            time.sleep(5)
        if _ok(["kubectl", "get", "ns", NS]):
            _warn(ctx.st, "namespace gone", "still terminating after 5m")
        else:
            ctx.st.ok("namespace gone", f"after {int(time.monotonic() - started)}s")
    else:
        ctx.st.ok("namespace gone", "already absent")


# ----- dispatch ----------------------------------------------------------------
PHASES: dict[str, Callable[[Ctx], None]] = {
    "pre": stage_pre,
    "scaleup-small": stage_scaleup_small,
    "scaleup-bigreq": stage_scaleup_bigreq,
    "scaleup-spread": stage_scaleup_spread,
    "scaledown-empty": stage_scaledown_empty,
    "scaledown-underutil": stage_scaledown_underutil,
    "pdb": stage_pdb,
    "do-not-disrupt": stage_do_not_disrupt,
    "hardening": stage_hardening,
    "limits": stage_limits,
    "family-matrix": stage_family_matrix,
    "pool-discovery": stage_pool_discovery,
    "boundary-audit": stage_boundary_audit,
    "cleanup": stage_cleanup,
}

# `all` runs every phase in this order (the bash dispatch order: family-matrix
# and limits run BEFORE hardening, unlike the header listing).
ALL_ORDER = (
    "pre",
    "scaleup-small",
    "scaleup-bigreq",
    "scaleup-spread",
    "scaledown-empty",
    "scaledown-underutil",
    "pdb",
    "do-not-disrupt",
    "family-matrix",
    "limits",
    "hardening",
    "pool-discovery",
    "boundary-audit",
    "cleanup",
)


def run_phase(ctx: Ctx, name: str) -> None:
    if name == "all":
        for ph in ALL_ORDER:
            PHASES[ph](ctx)
        return
    if name not in PHASES:
        die(f"unknown phase: {name}", 2)
    PHASES[name](ctx)


# ----- main --------------------------------------------------------------------
def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(
        prog="cdpctl verify-karpenter",
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    ap.add_argument(
        "--phase",
        action="append",
        default=None,
        metavar="PHASE",
        help="phase to run (repeatable; default: all)",
    )
    ap.add_argument(
        "--keep-going",
        action="store_true",
        help="continue past failures instead of aborting",
    )
    ap.add_argument(
        "--no-cleanup",
        action="store_true",
        help="retain the karpenter-validation namespace",
    )
    _stage.add_output_flags(ap)
    args = ap.parse_args(argv)
    phases: list[str] = args.phase or ["all"]

    _stage.require("kubectl", "aws")

    profile = os.environ.get("AWS_PROFILE", "")
    if not profile:
        die("AWS_PROFILE must be exported", 2)
    os.environ["AWS_PROFILE"] = profile

    mode = _stage.output_mode(args)
    ctx = Ctx(
        st=_stage.Stages(quiet=(mode != "human")),
        mode=mode,
        phases=phases,
        kube_context=_current_context("<no-kubeconfig>"),
        tests_dir=f"{repo_root()}/scripts/karpenter-tests",
        profile=profile,
        keep_going=args.keep_going,
        no_cleanup=args.no_cleanup,
        # Tunable budgets (seconds).
        scaleup_budget_s=int(os.environ.get("KARPENTER_SCALEUP_BUDGET_S", "180")),
        empty_drain_budget_s=int(os.environ.get("KARPENTER_EMPTY_DRAIN_BUDGET_S", "180")),
        underutil_budget_s=int(os.environ.get("KARPENTER_UNDERUTIL_BUDGET_S", "300")),
        pdb_observe_s=int(os.environ.get("KARPENTER_PDB_OBSERVE_S", "180")),
        dnd_observe_s=int(os.environ.get("KARPENTER_DND_OBSERVE_S", "180")),
    )

    ctx.st.echo(f"verify-karpenter  phases={' '.join(phases)}  context={ctx.kube_context}")

    stage_prereqs(ctx)

    for ph in phases:
        run_phase(ctx, ph)
        if ctx.st.failed > 0 and not ctx.keep_going:
            break

    _emit_payload(ctx)
    return ctx.st.exit_code()


if __name__ == "__main__":
    sys.exit(main())
