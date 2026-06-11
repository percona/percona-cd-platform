#!/usr/bin/env python3
"""Gated runbook automations behind `just runbook`.

One subcommand per runbook from docs/runbooks/common-operations.md. Two
modes: AUTOMATED subcommands enforce their gates mechanically and stop on
any mismatch. GUIDED subcommands walk the operator through the steps,
running each command only after confirmation.

Read the prose first; this tool encodes it, it does not replace it.
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import sys

from cdpctl._repo import repo_root


def run(cmd: list[str], **kw) -> subprocess.CompletedProcess:
    print(f"+ {' '.join(cmd)}", file=sys.stderr)
    return subprocess.run(cmd, cwd=repo_root(), **kw)


def die(msg: str, code: int = 3):
    print(f"GATE FAILED: {msg}", file=sys.stderr)
    sys.exit(code)


def confirm(prompt: str, assume_yes: bool) -> bool:
    if assume_yes:
        print(f"{prompt} [auto-yes]")
        return True
    if not sys.stdin.isatty():
        die(f"interactive confirmation needed ({prompt!r}); rerun with --yes or from a TTY")
    return input(f"{prompt} [y/N] ").strip().lower() == "y"


def gate_clean_main():
    """The runbooks start post-merge: a clean checkout of origin/main."""
    run(["git", "fetch", "origin", "-q"])
    head = subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=repo_root(), text=True).strip()
    main = subprocess.check_output(
        ["git", "rev-parse", "origin/main"], cwd=repo_root(), text=True
    ).strip()
    dirty = [
        line
        for line in subprocess.check_output(
            ["git", "status", "--porcelain"], cwd=repo_root(), text=True
        ).splitlines()
        # tofu init refreshes platform hashes in the tracked lock file on
        # every operator machine; that is not a content change to gate on.
        if line.strip() and not line.endswith("terraform/.terraform.lock.hcl")
    ]
    if head != main or dirty:
        for line in dirty:
            print(f"  dirty: {line}", file=sys.stderr)
        die(
            "run from a clean checkout of origin/main (merge the PR first; runbooks start post-merge)"
        )


def tf_plan() -> dict:
    if run(["just", "tf-plan"], stdout=subprocess.DEVNULL).returncode != 0:
        die("tf-plan failed")
    out = subprocess.check_output(
        ["tofu", "-chdir=terraform", "show", "-json", "tfplan"], cwd=repo_root()
    )
    return json.loads(out)


def plan_changes(plan: dict) -> list[tuple[str, list[str]]]:
    out = []
    for rc in plan.get("resource_changes", []):
        actions = [a for a in rc["change"]["actions"] if a not in ("no-op", "read")]
        if actions:
            out.append((rc["address"], actions))
    return out


def scope_violations(
    changes: list[tuple[str, list[str]]], inst: str
) -> list[tuple[str, list[str]]]:
    """Plan-scope gate: only that master's init-config S3 objects may update."""
    expected_prefix = f"module.{inst}.aws_s3_object.init_config"
    return [
        (a, acts)
        for a, acts in changes
        if not (a.startswith(expected_prefix) and acts == ["update"])
    ]


def evaluate_live(inst: str, src: str, assume_yes: bool):
    if shutil.which("jenkins") is None:
        print("jenkins CLI not found. Either wait for the 30 min SSM sync plus the")
        print("next JVM start, or evaluate manually:")
        print(f"  jenkins admin -i {inst} groovy -f {src}")
        return
    if confirm(f"Evaluate {src} on the live {inst} JVM now?", assume_yes):
        if run(["jenkins", "admin", "-i", inst, "groovy", "-f", src]).returncode != 0:
            die("live evaluate failed; the S3 copy is updated, the running JVM is not")


# ---------------- automated: template-change ----------------


def rb_template_change(args):
    inst, fname = args.inst, args.file
    src = f"resources/jenkins-masters/{inst}/init.groovy.d/{fname}"
    src_disk = os.path.join(repo_root(), src)
    if not os.path.exists(src_disk):
        tftpl = src_disk + ".tftpl"
        if os.path.exists(tftpl):
            die(
                f"{src} is templated ({fname}.tftpl); the rendered copy lives in S3. "
                "Evaluate from the bucket after apply, not from the repo file"
            )
        die(f"no such file: {src}")
    gate_clean_main()
    plan = tf_plan()
    changes = plan_changes(plan)
    bad = scope_violations(changes, inst)
    if not changes:
        print("Plan is empty: the merged change is already applied. Nothing to do.")
    elif bad:
        for a, acts in bad:
            print(f"  unexpected: {a} {acts}", file=sys.stderr)
        die(
            f"plan contains changes beyond {inst}'s init-config objects; "
            "resolve or apply those deliberately first"
        )
    else:
        for a, _ in changes:
            print(f"  will update: {a}")
        if not confirm("Apply (S3 objects only, no instance impact)?", args.yes):
            sys.exit(0)
        if run(["just", "tf-apply"]).returncode != 0:
            die("tf-apply failed")
    # The saved plan is consumed (or moot); tofu rejects stale plans anyway,
    # so a lingering terraform/tfplan only confuses the next run.
    plan_file = os.path.join(repo_root(), "terraform", "tfplan")
    if os.path.exists(plan_file):
        os.remove(plan_file)
    evaluate_live(inst, src, args.yes)
    print("\nVERIFY:")
    print("  1. Probe build on the changed label (the only real proof).")
    print("  2. just tf-plan returns 'No changes'.")


# ---------------- guided runbooks ----------------

GUIDED = {
    "master-resize": [
        (
            "Announce the window in #opensource-jenkins (instance replacement: builds in flight die)",
            None,
        ),
        ("Edit on_demand_instance_type in terraform/master-<inst>.tf, merge the PR", None),
        (
            "Plan and review: expect the instance to REPLACE and the identity volume to re-attach",
            ["just", "tf-plan"],
        ),
        ("Apply", ["just", "tf-apply"]),
        (
            "Wait for the endpoint reconciler to converge (about a minute after :8080 serves), then check the UI",
            None,
        ),
        ("Confirm telemetry", ["just", "check-master-alloy"]),
    ],
    "core-bump": [
        (
            "Announce the window in #opensource-jenkins (user-data change: instance replacement)",
            None,
        ),
        ("Edit jenkins_package_version in terraform/master-<inst>.tf, merge the PR", None),
        ("Plan and review: expect the instance to REPLACE", ["just", "tf-plan"]),
        ("Apply", ["just", "tf-apply"]),
        ("Verify the master serves and plugins loaded (0 failed)", None),
        ("Confirm telemetry", ["just", "check-master-alloy"]),
    ],
    "ssh-key": [
        ("Edit ssh_key_engineers in terraform/master-<inst>.tf, merge the PR", None),
        (
            "Plan and apply (takes effect at the NEXT instance replacement, not immediately)",
            ["just", "tf-plan"],
        ),
        (
            "For urgent removal, also strip the key from the running master over SSM: "
            "just ssm-run <inst> 'sed -i /<pattern>/d /home/ec2-user/.ssh/authorized_keys'",
            None,
        ),
    ],
}


def rb_guided(name: str, args):
    steps = GUIDED[name]
    print(f"Guided runbook: {name} (docs/runbooks/common-operations.md)\n")
    for i, (desc, cmd) in enumerate(steps, 1):
        print(f"Step {i}/{len(steps)}: {desc}")
        if cmd is None:
            if not confirm("  done?", args.yes):
                sys.exit(0)
            continue
        if confirm(f"  run: {' '.join(cmd)} ?", args.yes):
            if run(cmd).returncode != 0:
                die(f"step {i} failed")
        else:
            print("  skipped")
    print("\nRunbook complete.")


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="cdpctl runbook", description=__doc__)
    sub = ap.add_subparsers(dest="action")
    tc = sub.add_parser(
        "template-change",
        help="worker-template change, post-merge (AUTOMATED: plan gate, apply, live evaluate)",
    )
    tc.add_argument("inst")
    tc.add_argument(
        "--file",
        default="cloud.groovy",
        help="init.groovy.d file (default cloud.groovy; htz.cloud.groovy, ec2FleetCloud.groovy, matrix.groovy)",
    )
    tc.add_argument("--yes", action="store_true")
    for name in GUIDED:
        g = sub.add_parser(name, help=f"{name} (GUIDED: confirm each step)")
        g.add_argument("--yes", action="store_true")
    args = ap.parse_args(argv)
    if args.action is None:
        print("runbook subcommands:")
        print("  template-change <inst> [--file F]   AUTOMATED  worker-template change")
        for name in GUIDED:
            print(f"  {name:<35} GUIDED")
        print("\nProse: docs/runbooks/common-operations.md")
        return 0
    if args.action == "template-change":
        rb_template_change(args)
    else:
        rb_guided(args.action, args)
    return 0


if __name__ == "__main__":
    sys.exit(main())
