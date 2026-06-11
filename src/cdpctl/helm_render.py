"""CI gate: render the per-instance Jenkins controller chart and assert the
values actually reach the upstream `jenkins` subchart. Regression guard for the
top-level-vs-`jenkins:`-scoped bug that silently left ps3-k8s on chart defaults
(resources/jenkins/master has no real templates/, so top-level `controller:`
values went nowhere; they MUST be nested under the `jenkins:` dependency key).

Renders values-base.yaml alone, then base + each instances/<host> and each
_disabled/<host> overlay, and asserts the controller actually came up as OUR
image on the dedicated node pool with the Retain PVC + JNLP listener.

Run via: just helm-render
"""

from __future__ import annotations

import glob
import os
import re
import subprocess
import sys

from cdpctl import _stage
from cdpctl._repo import repo_root


def _helm_dependency_build(chart: str) -> None:
    devnull = subprocess.DEVNULL
    if (
        subprocess.run(
            ["helm", "dependency", "build"], cwd=chart, stdout=devnull, stderr=devnull
        ).returncode
        != 0
    ):
        subprocess.run(
            ["helm", "dependency", "update"], cwd=chart, stdout=devnull, stderr=devnull, check=False
        )


class _Gate:
    def __init__(self) -> None:
        self.fail = False

    def assert_in(self, name: str, needle: str, out: str) -> None:
        if needle not in out:
            print(f"  FAIL [{name}]: missing '{needle}'")
            self.fail = True

    def refute_in(self, name: str, needle: str, out: str) -> None:
        if needle in out:
            print(f"  FAIL [{name}]: unexpected '{needle}'")
            self.fail = True


def render_check(gate: _Gate, chart: str, name: str, *value_files: str) -> None:
    cmd = ["helm", "template", name, chart]
    for vf in value_files:
        cmd += ["-f", vf]
    res = subprocess.run(cmd, capture_output=True, text=True)
    out = res.stdout + res.stderr
    if res.returncode != 0:
        print(f"  FAIL [{name}]: helm template errored")
        print("\n".join(out.splitlines()[-8:]))
        gate.fail = True
        return
    before = gate.fail
    gate.assert_in(
        name, "percona-cd/jenkins-percona", out
    )  # our controller image reached the subchart
    gate.assert_in(name, "kind: StatefulSet", out)
    # Persistence reaches the subchart as EITHER a templated AZ-pinned Retain PVC
    # (gp3-jenkins-1a-retain) OR a pre-bound restored-home existingClaim (the chart
    # then templates no PVC, so the storageClass string is absent by design).
    if not re.search(r"gp3-jenkins-1a-retain|claimName: .+-jenkins-home", out):
        print(
            f"  FAIL [{name}]: persistence not wired (no Retain SC and no *-jenkins-home existingClaim)"
        )
        gate.fail = True
    gate.assert_in(name, "group.name: jenkins-masters", out)  # shared ALB group
    gate.assert_in(
        name, "name: agent-listener", out
    )  # inbound JNLP listener for EC2/Hetzner agents
    gate.assert_in(
        name, "workload.percona.com/tier: jenkins-master", out
    )  # pinned to the dedicated node pool
    gate.refute_in(
        name, 'image: "jenkins/jenkins', out
    )  # upstream-default controller image must NOT win
    if not gate.fail and not before:
        print(f"  ok [{name}]")


def main(argv: list[str] | None = None) -> int:
    _stage.require("helm")
    repo = repo_root()
    chart = os.path.join(repo, "resources/jenkins/master")
    if not os.path.isfile(os.path.join(chart, "Chart.yaml")):
        print(f"run from repo root (no {chart}/Chart.yaml)")
        return 2

    _helm_dependency_build(chart)

    gate = _Gate()
    print("jenkins chart render-check:")
    render_check(gate, chart, "base", os.path.join(chart, "values-base.yaml"))
    overlays = sorted(glob.glob(os.path.join(chart, "instances/*/"))) + sorted(
        glob.glob(os.path.join(repo, "resources/jenkins/_disabled/*/"))
    )
    for inst in overlays:
        vf = os.path.join(inst, "values.yaml")
        if not os.path.isfile(vf):
            continue
        render_check(
            gate,
            chart,
            os.path.basename(inst.rstrip("/")),
            os.path.join(chart, "values-base.yaml"),
            vf,
        )

    if not gate.fail:
        print("PASS")
        return 0
    print("FAIL")
    return 1


if __name__ == "__main__":
    sys.exit(main())
