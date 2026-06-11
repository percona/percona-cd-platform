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

import argparse
import glob
import json
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
    """Collects per-render assertion failures, reported as one stage row each."""

    def __init__(self, st) -> None:
        self.st = st

    @property
    def fail(self) -> bool:
        return self.st.failed > 0

    def assert_in(self, name: str, needle: str, out: str) -> None:
        if needle not in out:
            self.st.fail(name, f"missing '{needle}'")

    def refute_in(self, name: str, needle: str, out: str) -> None:
        if needle in out:
            self.st.fail(name, f"unexpected '{needle}'")


def render_check(gate: _Gate, chart: str, name: str, *value_files: str) -> None:
    cmd = ["helm", "template", name, chart]
    for vf in value_files:
        cmd += ["-f", vf]
    res = subprocess.run(cmd, capture_output=True, text=True)
    out = res.stdout + res.stderr
    if res.returncode != 0:
        gate.st.fail(name, "helm template errored")
        gate.st.echo("\n".join(out.splitlines()[-8:]))
        return
    before = gate.st.failed
    gate.assert_in(
        name, "percona-cd/jenkins-percona", out
    )  # our controller image reached the subchart
    gate.assert_in(name, "kind: StatefulSet", out)
    # Persistence reaches the subchart as EITHER a templated AZ-pinned Retain PVC
    # (gp3-jenkins-1a-retain) OR a pre-bound restored-home existingClaim (the chart
    # then templates no PVC, so the storageClass string is absent by design).
    if not re.search(r"gp3-jenkins-1a-retain|claimName: .+-jenkins-home", out):
        gate.st.fail(
            name, "persistence not wired (no Retain SC and no *-jenkins-home existingClaim)"
        )
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
    if gate.st.failed == before:
        gate.st.ok(name, "values reach the jenkins subchart")


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="cdpctl helm-render-check", description=__doc__)
    _stage.add_output_flags(ap)
    args = ap.parse_args(argv)
    mode = _stage.output_mode(args)

    _stage.require("helm")
    repo = repo_root()
    chart = os.path.join(repo, "resources/jenkins/master")
    if not os.path.isfile(os.path.join(chart, "Chart.yaml")):
        print(f"no {chart}/Chart.yaml", file=sys.stderr)
        return 2

    _helm_dependency_build(chart)

    st = _stage.Stages(quiet=(mode != "human"))
    gate = _Gate(st)
    st.section("jenkins chart render-check")
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

    if mode == "json":
        print(json.dumps(st.envelope(chart="resources/jenkins/master"), indent=2))
    elif mode == "llm":
        st.emit_llm()
    else:
        st.echo(f"\n{st.passed} pass, {st.failed} fail")
    return st.exit_code()


if __name__ == "__main__":
    sys.exit(main())
