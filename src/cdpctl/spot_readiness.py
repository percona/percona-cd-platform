#!/usr/bin/env python3
"""Read-only audit of a Jenkins master's interrupt/restart readiness.

Focuses on the checks that matter for the master staying recoverable: SSM
reachability, cloud-init completion, jenkins.service + JVM rehydrate flags,
and the api-admin Secrets Manager + Jenkins auth path. On a SPOT master
(legacy CFN, e.g. pg) it additionally audits the spot-interrupt plumbing:
SpotFleet capacity rebalancing, the terminate-check cron watcher, and
graceful-stop.sh. On an on-demand master (the TF-managed fleet) those
spot-only stages SKIP with an explanatory row instead of failing against
infrastructure that no longer exists.

Output: PASS/FAIL/SKIP stage rows (human), --json envelope, or --llm
pipe-delimited rows. Exit 0 clean, 1 on failures, 2 on preconditions.
Requires aws on PATH and AWS_PROFILE exported.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys
import time
from collections.abc import Callable
from typing import Any

from cdpctl import _stage
from cdpctl._repo import die

# Region map for masters in known regions (matches percona-jenkins skill).
# Add new masters here when they move to Terraform.
MASTER_REGION = {
    "ps3": "eu-west-1",
    "rel": "eu-west-1",
    "cloud": "eu-west-1",
    "pxb": "us-west-2",
    "pmm": "us-east-2",
    "pg": "eu-central-1",
    "ps57": "eu-central-1",
    "pxc": "us-west-1",
    "ps80": "us-west-2",
    "psmdb": "us-west-2",
}

# Probe order when locating a bare i-... instance id without --region.
PROBE_REGIONS = ("eu-west-1", "us-west-2", "us-east-2", "eu-central-1", "us-west-1")

# A terminal get-command-invocation status ends the poll. NOTE: like the bash
# ssm_run, ANY terminal status (including Failed) counts as a successful probe;
# stages judge the captured stdout, not the remote exit code.
TERMINAL_SSM_STATUSES = ("Success", "Failed", "TimedOut", "Cancelled")

type SsmRun = Callable[[str], tuple[bool, str, str]]


def _aws_json(argv: list[str]) -> Any:
    """`aws ... --output json` with stderr discarded; None when the call fails."""
    try:
        res = subprocess.run([*argv, "--output", "json"], capture_output=True, text=True)
    except OSError:
        return None
    if res.returncode != 0 or not res.stdout.strip():
        return None
    try:
        return json.loads(res.stdout)
    except json.JSONDecodeError:
        return None


def _dig(obj: Any, *path: str | int) -> Any:
    """Safe JMESPath-ish descent (Reservations[0].Instances[0]...); None when absent."""
    for key in path:
        if isinstance(key, int):
            if not isinstance(obj, list) or len(obj) <= key:
                return None
            obj = obj[key]
        elif isinstance(obj, dict):
            obj = obj.get(key)
        else:
            return None
    return obj


def _instance_visible(region: str, profile: str, instance_id: str) -> bool:
    """Mirror the bash region probe: `aws ec2 describe-instances` exits 0 here."""
    try:
        res = subprocess.run(
            [
                "aws",
                "ec2",
                "describe-instances",
                "--region",
                region,
                "--profile",
                profile,
                "--instance-ids",
                instance_id,
                "--query",
                "Reservations[0].Instances[0].State.Name",
                "--output",
                "text",
            ],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
    except OSError:
        return False
    return res.returncode == 0


def _resolve_target(inst: str, region_flag: str, profile: str) -> tuple[str, str, str]:
    """inst (shortname or i-...) -> (shortname, instance_id, region).

    shortname is "" when the caller passed an instance id directly.
    """
    if inst.startswith("i-"):
        instance_id = inst
        region = region_flag
        if not region:
            # Try all known regions to find the instance.
            for r in PROBE_REGIONS:
                if _instance_visible(r, profile, instance_id):
                    region = r
                    break
            if not region:
                die(f"could not locate {instance_id} in any known region; pass --region", 2)
        return "", instance_id, region

    region = region_flag or MASTER_REGION.get(inst, "")
    if not region:
        die(f"no region for shortname '{inst}'; pass --region", 2)
    # Find running instance tagged with this master shortname.
    data = _aws_json(
        [
            "aws",
            "ec2",
            "describe-instances",
            "--region",
            region,
            "--profile",
            profile,
            "--filters",
            f"Name=tag:iit-billing-tag,Values=jenkins-{inst}",
            "Name=instance-state-name,Values=running",
        ]
    )
    instance_id = _dig(data, "Reservations", 0, "Instances", 0, "InstanceId")
    if not instance_id or instance_id == "None":
        die(f"no running instance with iit-billing-tag=jenkins-{inst} in {region}", 2)
    return inst, str(instance_id), region


def _instance_lifecycle(region: str, profile: str, instance_id: str) -> str:
    """ "spot" for spot instances, "on-demand" otherwise (InstanceLifecycle absent)."""
    data = _aws_json(
        [
            "aws",
            "ec2",
            "describe-instances",
            "--region",
            region,
            "--profile",
            profile,
            "--instance-ids",
            instance_id,
        ]
    )
    val = _dig(data, "Reservations", 0, "Instances", 0, "InstanceLifecycle")
    return "spot" if val == "spot" else "on-demand"


def _ssm_run(region: str, profile: str, instance_id: str, cmd: str) -> tuple[bool, str, str]:
    """Run a shell command on the instance via SSM (port of the bash ssm_run).

    Returns (reached_terminal, status, stdout). reached_terminal mirrors the
    bash helper's exit status: True for ANY terminal invocation status, False
    only when send-command fails (status "") or the poll gives up (status
    "TIMEOUT"). stdout has trailing newlines stripped, like `$(...)` did.
    """
    send = _aws_json(
        [
            "aws",
            "ssm",
            "send-command",
            "--region",
            region,
            "--profile",
            profile,
            "--instance-ids",
            instance_id,
            "--document-name",
            "AWS-RunShellScript",
            "--parameters",
            json.dumps({"commands": [cmd]}),
        ]
    )
    cid = _dig(send, "Command", "CommandId")
    if not cid:
        return False, "", ""
    for _ in range(30):
        time.sleep(2)
        inv = _aws_json(
            [
                "aws",
                "ssm",
                "get-command-invocation",
                "--region",
                region,
                "--profile",
                profile,
                "--command-id",
                str(cid),
                "--instance-id",
                instance_id,
            ]
        )
        if inv is None:
            continue
        status = _dig(inv, "Status")
        if status not in TERMINAL_SSM_STATUSES:
            continue
        stdout = _dig(inv, "StandardOutputContent") or ""
        return True, str(status), str(stdout).rstrip("\n")
    return False, "TIMEOUT", ""


def _check_spotfleet(st: _stage.Stages, region: str, profile: str, instance_id: str) -> None:
    st.section("SpotFleet / Capacity Rebalancing")

    desc = _aws_json(
        [
            "aws",
            "ec2",
            "describe-instances",
            "--region",
            region,
            "--profile",
            profile,
            "--instance-ids",
            instance_id,
        ]
    )
    sfr_id = None
    for tag in _dig(desc, "Reservations", 0, "Instances", 0, "Tags") or []:
        if isinstance(tag, dict) and tag.get("Key") == "aws:ec2spot:fleet-request-id":
            sfr_id = tag.get("Value")
            break
    if not sfr_id or sfr_id == "None":
        st.fail("instance not part of a SpotFleet", "tag aws:ec2spot:fleet-request-id missing")
        return
    st.ok("SpotFleet membership", str(sfr_id))

    sfr = _aws_json(
        [
            "aws",
            "ec2",
            "describe-spot-fleet-requests",
            "--region",
            region,
            "--profile",
            profile,
            "--spot-fleet-request-ids",
            str(sfr_id),
        ]
    )
    cfg = _dig(sfr, "SpotFleetRequestConfigs", 0, "SpotFleetRequestConfig")
    rebal = _dig(cfg, "SpotMaintenanceStrategies", "CapacityRebalance", "ReplacementStrategy")
    if not rebal or rebal == "None":
        st.fail(
            "Capacity Rebalancing not enabled", "no SpotMaintenanceStrategies.CapacityRebalance"
        )
    else:
        st.ok("Capacity Rebalancing enabled", f"replacement_strategy={rebal}")

    lt_ver = _dig(cfg, "LaunchTemplateConfigs", 0, "LaunchTemplateSpecification", "Version")
    if lt_ver == "$Latest":
        st.ok("SpotFleet pinned to $Latest", "userdata edits roll on next replacement")
    else:
        lt_text = "" if sfr is None else ("None" if lt_ver is None else str(lt_ver))
        st.fail(
            f"SpotFleet pinned to LT version {lt_text}",
            "should be $Latest; userdata edits will not propagate",
        )


def _check_ssm_agent(st: _stage.Stages, region: str, profile: str, instance_id: str) -> bool:
    """False = SSM unreachable; caller prints the abbreviated summary and exits 1."""
    st.section("SSM agent")
    info = _aws_json(
        [
            "aws",
            "ssm",
            "describe-instance-information",
            "--region",
            region,
            "--profile",
            profile,
            "--filters",
            f"Key=InstanceIds,Values={instance_id}",
        ]
    )
    ping = _dig(info, "InstanceInformationList", 0, "PingStatus")
    ping_text = "" if info is None else ("None" if ping is None else str(ping))
    if ping_text == "Online":
        st.ok("SSM ping", "Online")
        return True
    st.fail("SSM ping", f"status={ping_text} (cannot proceed with in-instance checks)")
    return False


def _check_cloud_init(st: _stage.Stages, run: SsmRun) -> None:
    st.section("cloud-init")
    reached, status, out = run("cloud-init status --long 2>&1 | head -2")
    if reached:
        if "status: done" in out:
            st.ok("cloud-init status", "done")
        else:
            st.fail("cloud-init status", out.split("\n", 1)[0])
    else:
        st.fail("cloud-init probe failed", status)


def _check_cron(st: _stage.Stages, run: SsmRun) -> None:
    st.section("cron / spot-interrupt watcher")

    reached, _status, out = run("systemctl is-active crond; systemctl is-enabled crond")
    if reached:
        lines = out.split("\n")
        cron_active = lines[0]
        cron_enabled = lines[1] if len(lines) > 1 else ""
        if cron_active == "active" and cron_enabled == "enabled":
            st.ok("crond.service", "active + enabled")
        else:
            st.fail("crond.service", f"active={cron_active} enabled={cron_enabled}")

    reached, _status, out = run(
        "test -f /etc/cron.d/terminate-check && cat /etc/cron.d/terminate-check"
    )
    if reached:
        if "/usr/local/bin/jenkins-graceful-stop.sh" in out:
            st.ok("/etc/cron.d/terminate-check", "calls jenkins-graceful-stop.sh")
        else:
            st.fail(
                "/etc/cron.d/terminate-check",
                "content does not reference jenkins-graceful-stop.sh",
            )
    else:
        st.fail("/etc/cron.d/terminate-check", "file missing")

    # Recent cron firings (proves daemon is parsing the file).
    reached, _status, out = run(
        'journalctl -u crond --since "5 min ago" --no-pager 2>/dev/null | grep -c terminate-check'
    )
    if reached:
        fires = out.replace("\n", "")
        try:
            hits = int(fires or "0")
        except ValueError:
            hits = 0  # bash arithmetic on a non-number fails the -gt test
        if hits > 0:
            st.ok("cron recent firings", f"{fires} hits of terminate-check in last 5min")
        else:
            st.skip(
                "cron recent firings",
                "no terminate-check hits in last 5min (cron may have started < 1min ago)",
            )


def _check_graceful_stop(st: _stage.Stages, run: SsmRun) -> None:
    st.section("graceful-stop.sh")

    reached, _status, out = run(
        "test -x /usr/local/bin/jenkins-graceful-stop.sh"
        " && cat /usr/local/bin/jenkins-graceful-stop.sh"
    )
    if reached:
        st.ok("jenkins-graceful-stop.sh exists + executable")
        if any(re.search(r"^[ \t\r\f\v]*flock\b|^exec 9>", line) for line in out.split("\n")):
            st.ok("graceful-stop has flock guard", "no concurrent-drain pile-ups")
        else:
            st.fail(
                "graceful-stop missing flock guard",
                "concurrent cron firings will race after safeExit",
            )
        if "X-aws-ec2-metadata-token" in out:
            st.ok("graceful-stop uses IMDSv2", "tokens negotiated before metadata reads")
        else:
            st.fail(
                "graceful-stop uses IMDSv1",
                "LT requires IMDSv2; script will fail on token-required IMDS",
            )
    else:
        st.fail("jenkins-graceful-stop.sh", "missing or not executable")

    reached, _status, _out = run("command -v jq && jq --version 2>&1 | head -1")
    if reached:
        st.ok("jq installed", "graceful-stop dependency")
    else:
        st.fail("jq missing", "graceful-stop busyExecutors poll will fail")


def _check_jenkins_service(st: _stage.Stages, run: SsmRun) -> None:
    st.section("jenkins.service + JVM args")

    reached, _status, out = run("systemctl is-active jenkins")
    if reached:
        if "".join(out.split()) == "active":
            st.ok("jenkins.service", "active")
        else:
            st.fail("jenkins.service", f"state={out}")

    reached, _status, out = run(
        "ps -ef | grep jenkins | grep -v grep"
        " | grep -oE -- '-Dhetzner.rehydrate[^ ]+' || echo MISSING"
    )
    if reached:
        if re.search(r"rehydrate.enabled=true", out):
            # `tr '\n' ' ' | head -c 100` turned echo's trailing newline into a
            # trailing space; mirror it.
            st.ok("rehydrate flag in JVM args", (out.replace("\n", " ") + " ")[:100])
        else:
            st.skip("rehydrate flag", "not set; only required for eks_observability profile")


def _check_auth(st: _stage.Stages, run: SsmRun, region: str, inst: str) -> None:
    st.section("api-admin SM + Jenkins auth")

    sm_get = (
        f"TOKEN=$(aws secretsmanager get-secret-value --region {region} "
        f"--secret-id {inst}.cd/jenkins/admin-api-token --query SecretString --output text"
    )

    reached, _status, out = run(sm_get + ' 2>&1); echo "sm-exit=$?"; echo "token-len=${#TOKEN}"')
    if reached:
        if "sm-exit=0" in out:
            tok_len = next((ln for ln in out.split("\n") if "token-len" in ln), "")
            st.ok("Secrets Manager fetch", f"via instance IAM role, {tok_len}")
        else:
            st.fail("Secrets Manager fetch failed", "|".join(out.split("\n")[:3]) + "|")

    reached, _status, out = run(
        sm_get + " 2>/dev/null); "
        'curl -fsS -u "api-admin:$TOKEN" -o /dev/null -w "http=%{http_code}\\n" '
        "http://127.0.0.1:8080/whoAmI/api/json 2>&1 | head -1"
    )
    if reached:
        if "http=200" in out:
            st.ok("Jenkins api-admin auth probe", "loopback returns 200")
        else:
            st.fail("Jenkins api-admin auth probe", out.split("\n", 1)[0])

    reached, _status, out = run(
        sm_get + " 2>/dev/null); "
        'curl -fsS -u "api-admin:$TOKEN" "http://127.0.0.1:8080/computer/api/json?'
        'tree=busyExecutors" 2>&1 | jq -r .busyExecutors'
    )
    if reached:
        busy = "".join(out.split())
        if re.fullmatch(r"[0-9]+", busy):
            st.ok("busyExecutors endpoint", f"drain prerequisite OK, current={busy}")
        else:
            st.fail("busyExecutors endpoint", f"got '{busy}'")


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(
        prog="cdpctl spot-readiness",
        description=(
            "Interrupt/restart readiness audit for a Jenkins master: SSM, cloud-init, "
            "jenkins.service + JVM flags, api-admin auth path; plus the spot-interrupt "
            "plumbing (SpotFleet, cron watcher, graceful-stop) on spot masters. "
            "Read-only. AWS_PROFILE must be exported."
        ),
    )
    ap.add_argument(
        "inst",
        metavar="inst-or-instance-id",
        help="master shortname (e.g. pg), or i-... directly",
    )
    ap.add_argument(
        "--region",
        default="",
        help="override AWS region (default: derive from instance/profile)",
    )
    _stage.add_output_flags(ap)
    args = ap.parse_args(argv)

    _stage.require("aws")

    profile = os.environ.get("AWS_PROFILE", "")
    if not profile:
        die("AWS_PROFILE must be exported", 2)
    shortname, instance_id, region = _resolve_target(args.inst, args.region, profile)
    lifecycle = _instance_lifecycle(region, profile, instance_id)

    mode = _stage.output_mode(args)
    st = _stage.Stages(quiet=(mode != "human"))
    st.echo(
        f"checking master '{shortname or '<by-id>'}' "
        f"(instance {instance_id} in {region}, {lifecycle})"
    )

    def run(cmd: str) -> tuple[bool, str, str]:
        return _ssm_run(region, profile, instance_id, cmd)

    def finish(code: int) -> int:
        if mode == "json":
            print(
                json.dumps(
                    st.envelope(
                        master=shortname or instance_id,
                        instance=instance_id,
                        region=region,
                        lifecycle=lifecycle,
                    ),
                    indent=2,
                )
            )
        elif mode == "llm":
            st.emit_llm()
        return code

    spot = lifecycle == "spot"
    if spot:
        _check_spotfleet(st, region, profile, instance_id)
    else:
        st.section("SpotFleet / Capacity Rebalancing")
        st.skip("spot-interrupt plumbing", "master is on-demand; spot stages n/a")
    if not _check_ssm_agent(st, region, profile, instance_id):
        st.section("Summary")
        st.echo(f"  {st.passed} pass, {st.failed} fail, {st.skipped} skip")
        return finish(1)
    _check_cloud_init(st, run)
    if spot:
        _check_cron(st, run)
        _check_graceful_stop(st, run)
    else:
        st.section("cron / spot-interrupt watcher")
        st.skip("terminate-check cron", "master is on-demand; spot stages n/a")
        st.section("graceful-stop.sh")
        st.skip("graceful-stop.sh", "master is on-demand; spot stages n/a")
    _check_jenkins_service(st, run)
    if spot:
        _check_auth(st, run, region, args.inst)
    else:
        st.section("api-admin SM + Jenkins auth")
        st.skip("self-drain auth path", "master is on-demand; spot stages n/a")

    st.section("Summary")
    total = st.passed + st.failed + st.skipped
    st.echo(
        f"  {_stage.green(str(st.passed))} pass, {_stage.red(str(st.failed))} fail, "
        f"{_stage.gray(str(st.skipped))} skip (of {total})"
    )
    if st.failed > 0:
        st.echo("  -> instance will NOT respond correctly to an interrupt")
        return finish(1)
    st.echo("  -> instance is ready to respond to an interrupt")
    return finish(0)


if __name__ == "__main__":
    sys.exit(main())
