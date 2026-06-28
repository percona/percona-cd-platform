#!/usr/bin/env python3
"""Arch-pin gate (credential-free; runs in `just ci`).

Scans the committed cluster manifests for any `kubernetes.io/arch` that pins a
non-target arch, in either form:

  1. nodeSelector / node affinity:   kubernetes.io/arch: amd64
  2. Karpenter NodePool requirement: - key: kubernetes.io/arch
                                       values: [amd64]

The fleet is all-arm64, so any committed amd64 (or other non-arm64) arch pin in
this tree is a pod that will strand Pending. Fail-closed.

LIMITATION (by design): this only sees what is COMMITTED. An arch pin that lives
in an upstream chart's DEFAULT values (e.g. snapscheduler's upstream
`nodeSelector: kubernetes.io/arch: amd64`) is invisible to a static scan and is
caught instead by the live preflight `scripts/check-arch-readiness.sh`, which
renders/inspects effective state. Run that before any arch cutover.

Usage:
  uv run --with pyyaml python3 scripts/check_arch_pins.py            # target arm64
  uv run --with pyyaml python3 scripts/check_arch_pins.py amd64      # other target

Exit 0 if no wrong-arch pin is found, 1 otherwise.
"""

from __future__ import annotations

import sys
from pathlib import Path

import yaml

ARCH_KEY = "kubernetes.io/arch"
# Directories of committed manifests that ArgoCD syncs to the cluster. The
# clouds-catalog is JCasC config for dynamic BUILD AGENTS (legitimately amd64),
# not cluster PodSpecs, so it is excluded.
SCAN_DIRS = ("resources", "argocd-bootstrap")
EXCLUDE_SUBSTR = ("/clouds-catalog/",)


def arch_values(node: object) -> list[tuple[str, object]]:
    """Recursively collect (form, value) for every kubernetes.io/arch pin."""
    found: list[tuple[str, object]] = []
    if isinstance(node, dict):
        if ARCH_KEY in node:
            found.append(("nodeSelector", node[ARCH_KEY]))
        if node.get("key") == ARCH_KEY and "values" in node:
            found.append(("requirement", node["values"]))
        for value in node.values():
            found.extend(arch_values(value))
    elif isinstance(node, list):
        for item in node:
            found.extend(arch_values(item))
    return found


def offending(value: object, target: str) -> list[str]:
    """Return the pin if it EXCLUDES the target arch, else empty.

    A scalar nodeSelector (`arch: amd64`) excludes the target when it differs.
    A value list (`In [amd64, arm64]`) is a permitted SET, so it only excludes
    the target when the target is absent from it (a multi-arch list that
    includes arm64 is fine).
    """
    if isinstance(value, list):
        vals = [str(v) for v in value]
        return [] if target in vals else vals
    return [str(value)] if str(value) != target else []


def main() -> int:
    target = sys.argv[1] if len(sys.argv) > 1 else "arm64"
    repo_root = Path(__file__).resolve().parent.parent

    findings: list[str] = []
    scanned = 0

    for scan_dir in SCAN_DIRS:
        for path in sorted((repo_root / scan_dir).rglob("*.yaml")):
            rel = str(path.relative_to(repo_root))
            if any(sub in f"/{rel}" for sub in EXCLUDE_SUBSTR):
                continue
            text = path.read_text(encoding="utf-8", errors="replace")
            # Skip Helm templates (not parseable as YAML); the live preflight
            # covers rendered chart output.
            if "{{" in text:
                continue
            try:
                docs = list(yaml.safe_load_all(text))
            except yaml.YAMLError:
                continue
            scanned += 1
            for doc in docs:
                for form, value in arch_values(doc):
                    for bad in offending(value, target):
                        findings.append(f"{rel}: {form} pins {ARCH_KEY}={bad}")

    if findings:
        print(f"check_arch_pins: FAIL (target {target}) -- wrong-arch pins:")
        for finding in sorted(set(findings)):
            print(f"  - {finding}")
        return 1

    print(f"check_arch_pins: OK ({scanned} manifests scanned, target {target})")
    return 0


if __name__ == "__main__":
    sys.exit(main())
