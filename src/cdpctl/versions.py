#!/usr/bin/env python3
"""Check pinned versions against upstream latest.

This script holds NO hardcoded version pins of its own. The previous version
did, and that second copy silently drifted from the files it was meant to
verify (e.g. a stale tflint pin, and tools shown "TBD" while actually pinned in
.pre-commit-config.yaml). Pins are now READ from their real sources:

  terraform/versions.tf
      required_version (OpenTofu), required_providers, local.modules, local.charts
  resources/addons/*/Chart.yaml, resources/jenkins/master/Chart.yaml
      Helm chart dependencies (name, version, repository)
  resources/addons/{grafana,authentik}/values.yaml
      pinned container image tags (the deployed app version)
  .pre-commit-config.yaml
      pinned hook repo revs

"Latest" is derived from each pin's own metadata - the Helm repo index.yaml, the
Terraform registry, or GitHub releases - so adding or bumping a pin in those
files needs no edit here. A chart pinned in BOTH versions.tf and an addon
Chart.yaml is cross-checked; a mismatch is reported as DRIFT.

Run via: just check-versions  (uv supplies pyyaml + python-hcl2)
"""

from __future__ import annotations

import argparse
import glob
import json
import os
import subprocess
import sys
import urllib.request
from dataclasses import dataclass

from cdpctl import _stage
from cdpctl._repo import http_json, load_yaml, repo_root

UA = {"User-Agent": "percona-cd-platform-check-versions"}

# The only non-derivable lookups. These are NOT version pins - they map a pin's
# location to where its upstream "latest" is published.
#   OCI Helm repos have no index.yaml, so latest comes from GitHub releases.
OCI_GH = {"oci://public.ecr.aws/karpenter": ("aws/karpenter-provider-aws", "v")}
#   Container image tags (values.yaml) -> GitHub releases repo.
IMAGE_GH = {"grafana": "grafana/grafana", "authentik": "goauthentik/authentik"}


def _norm(tag: str) -> str:
    """Normalize an upstream tag to a bare version: drop a path prefix
    (authentik publishes `version/2026.2.4`) and a leading `v`."""
    return str(tag).split("/")[-1].lstrip("v")


def _first(x):
    """python-hcl2 wraps repeated blocks in lists; unwrap defensively."""
    return x[0] if isinstance(x, list) else x


def _clean(s) -> str:
    """python-hcl2 returns string literals with their surrounding quotes."""
    return str(s).strip('"')


# ---------- upstream "latest" resolvers ----------
def gh_latest(owner_repo: str, prefix: str | None = None) -> str:
    out = subprocess.check_output(
        ["gh", "api", f"repos/{owner_repo}/releases?per_page=40"],
        stderr=subprocess.DEVNULL,
        timeout=30,
    )
    for rel in json.loads(out):
        if rel.get("draft") or rel.get("prerelease"):
            continue
        tag = rel["tag_name"]
        if prefix and not tag.startswith(prefix):
            continue
        if any(t in tag.lower() for t in ("alpha", "beta", "rc", "-pre")):
            continue
        return tag
    return "?"


def helm_latest(repo_url: str, chart: str) -> str:
    import yaml

    url = f"{repo_url.rstrip('/')}/index.yaml"
    with urllib.request.urlopen(urllib.request.Request(url, headers=UA), timeout=25) as r:
        idx = yaml.safe_load(r)
    entries = idx.get("entries", {}).get(chart, [])
    for e in entries:  # index is semver-desc; take the first stable entry
        if "-" not in str(e.get("version", "")):
            return e["version"]
    return entries[0]["version"] if entries else "?"


def tf_registry_latest(kind: str, addr: str) -> str:
    # kind: "modules" (ns/name/provider) | "providers" (ns/name)
    url = f"https://registry.terraform.io/v1/{kind}/{addr}"
    return http_json(url, headers=UA, timeout=25).get("version", "?")


def _safe(fn, *a) -> str:
    try:
        return fn(*a)
    except Exception:
        return "ERR"


# ---------- pin readers ----------
def _yaml(path: str):
    return load_yaml(path) or {}


def read_versions_tf():
    import hcl2

    with open(f"{repo_root()}/terraform/versions.tf") as f:
        d = hcl2.load(f)

    def specs(m):  # drop hcl2 __comments__/__is_block__ metadata, keep real entries
        return {k: v for k, v in m.items() if not k.startswith("__") and isinstance(v, dict)}

    tf = _first(d["terraform"])
    rp = specs(_first(tf["required_providers"]))
    providers = {k: (_clean(v["source"]), _clean(v["version"])) for k, v in rp.items()}
    loc = _first(d["locals"])
    modules = {
        k: (_clean(v["source"]), _clean(v["version"])) for k, v in specs(loc["modules"]).items()
    }
    charts = {
        k: (_clean(v["repo"]), _clean(v["name"]), _clean(v["ver"]))
        for k, v in specs(loc["charts"]).items()
    }
    return _clean(tf["required_version"]), providers, modules, charts


def read_chart_deps():
    out = []  # (name, version, repo, source_label)
    paths = sorted(glob.glob(f"{repo_root()}/resources/addons/*/Chart.yaml"))
    paths.append(f"{repo_root()}/resources/jenkins/master/Chart.yaml")
    for p in paths:
        if not os.path.exists(p):
            continue
        label = os.path.relpath(p, repo_root())
        for dep in _yaml(p).get("dependencies") or []:
            out.append((dep["name"], dep["version"], dep["repository"], label))
    return out


def read_image_pins():
    out = []  # (label, tag, gh_repo)
    g = _yaml(f"{repo_root()}/resources/addons/grafana/values.yaml")
    tag = g.get("grafana", {}).get("image", {}).get("tag")
    if tag:
        out.append(("image/grafana", str(tag), IMAGE_GH["grafana"]))
    a = _yaml(f"{repo_root()}/resources/addons/authentik/values.yaml")
    tag = a.get("authentik", {}).get("global", {}).get("image", {}).get("tag")
    if tag:
        out.append(("image/authentik", str(tag), IMAGE_GH["authentik"]))
    return out


def read_precommit():
    out = []  # (owner_repo, rev)
    for r in _yaml(f"{repo_root()}/.pre-commit-config.yaml").get("repos", []):
        if r.get("repo") != "local" and r.get("rev"):
            out.append((r["repo"].split("github.com/", 1)[-1].rstrip("/"), r["rev"]))
    return out


# ---------- compare ----------
@dataclass
class Row:
    component: str
    pinned: str
    latest: str
    status: str
    source: str


def cmp_exact(pinned: str, latest: str) -> str:
    if latest in ("?", "ERR"):
        return "ERR"
    return "OK" if _norm(pinned) == _norm(latest) else "BUMP"


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="cdpctl versions", description=__doc__)
    _stage.add_output_flags(ap)
    args = ap.parse_args(argv)
    mode = _stage.output_mode(args)
    try:
        req_ver, providers, modules, charts = read_versions_tf()
    except Exception as e:
        print(f"FATAL: cannot parse terraform/versions.tf: {e}", file=sys.stderr)
        return 1

    rows: list[Row] = []
    drift: list[str] = []

    # Helm charts: union of versions.tf local.charts + every addon Chart.yaml
    # dependency, keyed by (repo, name) and cross-checked for intra-repo DRIFT.
    helm: dict[tuple[str, str], dict] = {}

    def add_chart(repo, name, ver, src):
        h = helm.setdefault((repo.rstrip("/"), name), {"versions": {}})
        h["versions"].setdefault(ver, []).append(src)

    for repo, name, ver in charts.values():
        add_chart(repo, name, ver, "versions.tf")
    for name, ver, repo, src in read_chart_deps():
        add_chart(repo, name, ver, src)

    for (repo, name), h in sorted(helm.items(), key=lambda kv: kv[0][1]):
        if repo in OCI_GH:
            owner_repo, pfx = OCI_GH[repo]
            latest = _norm(_safe(gh_latest, owner_repo, pfx))
        else:
            latest = _safe(helm_latest, repo, name)
        versions = h["versions"]
        if len(versions) > 1:
            drift.append(
                f"{name}: " + "; ".join(f"{v} <- {', '.join(s)}" for v, s in versions.items())
            )
            rows.append(Row(f"chart/{name}", "/".join(versions), latest, "DRIFT", "MULTIPLE"))
            continue
        ver = next(iter(versions))
        srcs = sorted({s for ss in versions.values() for s in ss})
        src = "versions.tf+Chart.yaml" if len(srcs) > 1 else srcs[0]
        rows.append(Row(f"chart/{name}", ver, latest, cmp_exact(ver, latest), src))

    # Terraform modules (registry latest; strip any //submodule path).
    for k, (source, ver) in sorted(modules.items()):
        latest = _safe(tf_registry_latest, "modules", source.split("//")[0])
        rows.append(Row(f"tf-module/{k}", ver, latest, cmp_exact(ver, latest), "versions.tf"))

    # Terraform providers + OpenTofu: version CONSTRAINTS, not exact pins -> INFO.
    for k, (source, constraint) in sorted(providers.items()):
        latest = _safe(tf_registry_latest, "providers", source)
        rows.append(Row(f"tf-provider/{k}", constraint, latest, "INFO", "versions.tf"))
    rows.append(
        Row(
            "opentofu",
            req_ver,
            _norm(_safe(gh_latest, "opentofu/opentofu", "v")),
            "INFO",
            "versions.tf",
        )
    )

    # Container image tags pinned in values.yaml.
    for label, tag, gh_repo in read_image_pins():
        latest = _norm(_safe(gh_latest, gh_repo))
        rows.append(Row(label, tag, latest, cmp_exact(tag, latest), "values.yaml"))

    # Pre-commit hook repos.
    for owner_repo, rev in read_precommit():
        latest = _safe(gh_latest, owner_repo)
        rows.append(
            Row(
                f"pre-commit/{owner_repo.split('/')[-1]}",
                _norm(rev),
                _norm(latest) if latest not in ("?", "ERR") else latest,
                cmp_exact(rev, latest),
                ".pre-commit-config.yaml",
            )
        )

    # ---------- emit ----------
    table = [[r.component, r.pinned, r.latest, r.status, r.source] for r in rows]
    counts: dict[str, int] = {}
    for r in rows:
        counts[r.status] = counts.get(r.status, 0) + 1
    _stage.emit_rows(
        ["component", "pinned", "latest", "status", "source"],
        table,
        mode,
        json_payload={
            "components": [r.__dict__ for r in rows],
            "summary": counts,
            "drift": drift,
        },
    )
    if mode == "human":
        print("\nsummary: " + "  ".join(f"{k}={v}" for k, v in sorted(counts.items())))
        if drift:
            print("\nDRIFT - same chart pinned to different versions across files:")
            for d in drift:
                print(f"  {d}")

        # ---------- EKS supported versions (advisory, human mode only) ----------
        print("\n=== EKS supported versions (aws eks describe-cluster-versions) ===")
        if not os.environ.get("AWS_PROFILE"):
            # AWS_PROFILE comes from the environment, never baked (repo convention).
            print("  (export AWS_PROFILE to include this section)")
        else:
            try:
                out = subprocess.check_output(
                    [
                        "aws",
                        "eks",
                        "describe-cluster-versions",
                        "--region",
                        "us-east-1",
                        "--output",
                        "json",
                    ],
                    stderr=subprocess.DEVNULL,
                    timeout=30,
                )
                eks_rows = [
                    [
                        cv["clusterVersion"],
                        cv["versionStatus"],
                        cv["kubernetesPatchVersion"],
                        cv["endOfStandardSupportDate"][:10],
                        cv["endOfExtendedSupportDate"][:10],
                        cv.get("defaultVersion", False),
                    ]
                    for cv in json.loads(out).get("clusterVersions", [])
                ]
                print(
                    _stage.render_table(
                        ["k8s", "status", "patch", "eos-std", "eos-ext", "default"], eks_rows
                    )
                )
            except Exception as e:
                print(f"  AWS EKS query failed: {e}")

    return 0


if __name__ == "__main__":
    sys.exit(main())
