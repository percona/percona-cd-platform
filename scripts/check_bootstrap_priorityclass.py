#!/usr/bin/env python3
"""Fail-closed gate: the two bootstrap PriorityClass copies must stay identical.

terraform/argocd.tf server-side-applies a bootstrap copy of the
platform-system-critical PriorityClass so ArgoCD can schedule on an empty
cluster; resources/addons/priorityclasses/ owns the class steady-state. SSA
co-ownership stays conflict-free only while the two definitions agree on the
semantic fields (value, globalDefault, preemptionPolicy, description), so any
divergence fails CI. ArgoCD-only metadata (the sync-wave annotation) is
excluded on purpose.

Run via: just bootstrap-priorityclass-check  (uv supplies pyyaml)
"""

import re
import sys
import textwrap
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parent.parent
TF_FILE = ROOT / "terraform/argocd.tf"
ADDON_FILE = ROOT / "resources/addons/priorityclasses/templates/priorityclasses.yaml"
CLASS_NAME = "platform-system-critical"
HEREDOC = re.compile(
    r'resource\s+"kubectl_manifest"\s+"argocd_priorityclass"\s*\{'
    r".*?yaml_body\s*=\s*<<-?YAML\n(.*?)\n\s*YAML",
    re.DOTALL,
)


def semantic_fields(doc: dict) -> dict:
    """Returns the fields both copies must agree on, whitespace-normalized."""
    fields = {key: doc.get(key) for key in ("value", "preemptionPolicy")}
    fields["description"] = " ".join(str(doc.get("description", "")).split())
    return fields


def load_copies() -> tuple[dict, dict]:
    """Returns the (terraform, addon) documents, exiting non-zero if either is missing."""
    match = HEREDOC.search(TF_FILE.read_text())
    if not match:
        sys.exit(
            f"FAIL: kubectl_manifest.argocd_priorityclass heredoc not found in {TF_FILE}"
        )
    tf_doc = yaml.safe_load(textwrap.dedent(match.group(1)))
    if tf_doc.get("metadata", {}).get("name") != CLASS_NAME:
        sys.exit(f"FAIL: terraform bootstrap copy is not named {CLASS_NAME}")
    for doc in yaml.safe_load_all(ADDON_FILE.read_text()):
        if doc and doc.get("metadata", {}).get("name") == CLASS_NAME:
            return tf_doc, doc
    sys.exit(f"FAIL: {CLASS_NAME} not found in {ADDON_FILE}")


def main() -> None:
    tf_doc, addon_doc = load_copies()

    # globalDefault is deliberately split, not shared: the terraform copy must
    # OMIT it (an explicit zero-value from a second SSA manager conflicts with
    # the field's owner, because the live object serializes false as absent),
    # while the addon copy must declare false so ArgoCD stays the sole owner
    # and corrects any drift to true. Equality alone would pass a symmetric
    # re-addition and silently re-arm the conflict.
    if "globalDefault" in tf_doc:
        sys.exit(
            "FAIL: the terraform bootstrap copy declares globalDefault; keep "
            "it omitted (explicit false conflicts under SSA with the field's "
            "current manager)"
        )
    if addon_doc.get("globalDefault") is not False:
        sys.exit(
            "FAIL: the addon copy must declare globalDefault: false so ArgoCD "
            "owns the field and reverts drift to true"
        )

    tf_fields, addon_fields = (semantic_fields(doc) for doc in (tf_doc, addon_doc))
    if tf_fields != addon_fields:
        diff = {
            key: (tf_fields[key], addon_fields[key])
            for key in tf_fields
            if tf_fields[key] != addon_fields[key]
        }
        sys.exit(
            f"FAIL: bootstrap PriorityClass copies diverge (terraform vs addon): {diff}"
        )
    print(f"OK: {CLASS_NAME} identical in terraform and addon")


if __name__ == "__main__":
    main()
