#!/usr/bin/env python3
"""Arch-pin gate (credential-free; runs in `just ci`).

Scans the committed cluster manifests for any node-arch constraint that EXCLUDES
the target arch, with correct Kubernetes selector semantics, in three forms:

  1. nodeSelector:            kubernetes.io/arch: amd64        (equality)
  2. required node affinity:  nodeSelectorTerms (ORed), each term a set of
                              matchExpressions (ANDed) on kubernetes.io/arch
                              with operator In / NotIn / Exists / DoesNotExist
  3. Karpenter NodePool:      requirements: - key: kubernetes.io/arch
                                              operator: In, values: [amd64]

The fleet is all-arm64, so any committed manifest that excludes arm64 is a pod
that will strand Pending. Fail-closed. `operator` is honoured: `In [arm64]` is
fine, `NotIn [arm64]` excludes it; an affinity is only an exclusion when EVERY
ORed term excludes the target.

LIMITATION (by design): this only sees what is COMMITTED, and skips Helm
templates (not valid YAML). An arch pin in an upstream chart's DEFAULT values
(e.g. snapscheduler's upstream amd64 nodeSelector) is invisible here and is
caught instead by the live preflight `scripts/check-arch-readiness.sh`.

Usage:
  uv run --with pyyaml python3 scripts/check_arch_pins.py            # target arm64
  uv run --with pyyaml python3 scripts/check_arch_pins.py amd64      # other target

Exit 0 if no exclusion is found, 1 otherwise.
"""

from __future__ import annotations

import sys
from pathlib import Path

import yaml

ARCH_KEY = "kubernetes.io/arch"
SCAN_DIRS = ("resources", "argocd-bootstrap")
EXCLUDE_SUBSTR = ("/clouds-catalog/",)


def expr_excludes(expr: dict, target: str) -> bool:
    """A single matchExpression / NodePool requirement on the arch key: does it
    exclude the target arch?"""
    operator = expr.get("operator", "In")
    values = [str(v) for v in (expr.get("values") or [])]
    if operator == "In":
        return target not in values
    if operator == "NotIn":
        return target in values
    if operator == "DoesNotExist":
        return True  # node must NOT carry the arch label: nothing matches
    if operator == "Exists":
        return False  # node carries the arch label: any arch permitted
    return False  # Gt/Lt are not meaningful for arch; ignore


def term_excludes(term: dict, target: str) -> bool:
    """A nodeSelectorTerm (AND of matchExpressions) excludes the target if any
    of its arch matchExpressions excludes it."""
    for expr in term.get("matchExpressions") or []:
        if isinstance(expr, dict) and expr.get("key") == ARCH_KEY and expr_excludes(expr, target):
            return True
    return False


def find_exclusions(node: object, target: str, out: list[str]) -> None:
    """Walk the doc and evaluate the three arch-pin structures with correct
    (operator- and OR-aware) semantics."""
    if isinstance(node, dict):
        selector = node.get("nodeSelector")
        if isinstance(selector, dict) and ARCH_KEY in selector and str(selector[ARCH_KEY]) != target:
            out.append(f"nodeSelector kubernetes.io/arch={selector[ARCH_KEY]}")

        terms = (
            ((node.get("affinity") or {}).get("nodeAffinity") or {})
            .get("requiredDuringSchedulingIgnoredDuringExecution") or {}
        ).get("nodeSelectorTerms")
        # ORed: the affinity excludes the target only if EVERY term excludes it.
        if isinstance(terms, list) and terms and all(term_excludes(t, target) for t in terms):
            out.append(f"nodeAffinity excludes kubernetes.io/arch (all {len(terms)} term(s))")

        requirements = node.get("requirements")
        if isinstance(requirements, list):
            for req in requirements:
                if isinstance(req, dict) and req.get("key") == ARCH_KEY and expr_excludes(req, target):
                    out.append(f"NodePool requirement excludes kubernetes.io/arch (operator {req.get('operator', 'In')})")

        for value in node.values():
            find_exclusions(value, target, out)
    elif isinstance(node, list):
        for item in node:
            find_exclusions(item, target, out)


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
            if "{{" in text:  # Helm template, not parseable; live preflight covers it
                continue
            try:
                docs = list(yaml.safe_load_all(text))
            except yaml.YAMLError:
                continue
            scanned += 1
            for doc in docs:
                doc_findings: list[str] = []
                find_exclusions(doc, target, doc_findings)
                findings.extend(f"{rel}: {f}" for f in doc_findings)

    if findings:
        print(f"check_arch_pins: FAIL (target {target}) -- manifests that exclude the target arch:")
        for finding in sorted(set(findings)):
            print(f"  - {finding}")
        return 1

    print(f"check_arch_pins: OK ({scanned} manifests scanned, target {target})")
    return 0


if __name__ == "__main__":
    sys.exit(main())
