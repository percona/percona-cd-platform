#!/usr/bin/env python3
"""Programmatically check latest versions of every pinned tool/component.
Sources: gh CLI (authenticated GitHub releases), Helm chart index.yaml, AWS CLI.
"""
from __future__ import annotations
import json
import subprocess
import sys
import urllib.request
from dataclasses import dataclass


def gh_releases(owner_repo: str, prefix: str | None = None, per_page: int = 30) -> list[tuple[str, str]]:
    out = subprocess.check_output(
        ["gh", "api", f"repos/{owner_repo}/releases?per_page={per_page}"],
        stderr=subprocess.DEVNULL,
        timeout=30,
    )
    rels = json.loads(out)
    rows = []
    for rel in rels:
        if rel.get("draft") or rel.get("prerelease"):
            continue
        tag = rel["tag_name"]
        if prefix and not tag.startswith(prefix):
            continue
        rows.append((tag, rel["published_at"][:10]))
    return rows


def helm_chart_latest(repo_url: str, chart: str) -> tuple[str, str]:
    import yaml  # type: ignore
    url = f"{repo_url.rstrip('/')}/index.yaml"
    req = urllib.request.Request(url, headers={"User-Agent": "version-check"})
    with urllib.request.urlopen(req, timeout=20) as r:
        idx = yaml.safe_load(r)
    entries = idx.get("entries", {}).get(chart, [])
    if not entries:
        return ("?", "?")
    e = entries[0]
    return (e["version"], e.get("created", "?")[:10])


@dataclass
class Pin:
    name: str
    pinned: str
    latest: str
    date: str
    status: str


def status(pinned: str, latest: str) -> str:
    if pinned == latest:
        return "OK"
    if pinned == "TBD":
        return "PIN"
    return "BUMP"


def main() -> int:
    print(f"{'Component':<42} {'Pinned':<14} {'Latest':<14} {'Date':<12} Status")
    print("-" * 92)

    rows: list[Pin] = []

    tf_modules = [
        ("terraform-aws-modules/vpc", "terraform-aws-modules/terraform-aws-vpc", "6.6.1"),
        ("terraform-aws-modules/eks", "terraform-aws-modules/terraform-aws-eks", "21.19.0"),
        ("terraform-aws-modules/iam", "terraform-aws-modules/terraform-aws-iam", "TBD"),
        ("terraform-aws-modules/eks-pod-identity", "terraform-aws-modules/terraform-aws-eks-pod-identity", "TBD"),
        ("terraform-aws-modules/acm", "terraform-aws-modules/terraform-aws-acm", "6.3.0"),
    ]
    for name, repo, pinned in tf_modules:
        rels = gh_releases(repo, prefix="v", per_page=5)
        latest = rels[0][0].lstrip("v") if rels else "?"
        date = rels[0][1] if rels else "?"
        rows.append(Pin(name, pinned, latest, date, status(pinned, latest)))

    helm_charts = [
        ("argo-cd chart", "argoproj/argo-helm", "argo-cd-", "9.5.9"),
        ("prometheus-operator-crds chart", "prometheus-community/helm-charts", "prometheus-operator-crds-", "28.0.1"),
        ("kube-state-metrics chart", "prometheus-community/helm-charts", "kube-state-metrics-", "7.3.0"),
        ("prometheus-node-exporter chart", "prometheus-community/helm-charts", "prometheus-node-exporter-", "4.55.0"),
        ("external-dns chart", "kubernetes-sigs/external-dns", "external-dns-helm-chart-", "1.21.1"),
    ]
    for name, repo, prefix, pinned in helm_charts:
        # The prometheus-community/helm-charts monorepo publishes >30
        # chart releases per week; per_page=100 keeps less-frequent charts
        # (e.g. prometheus-operator-crds) visible.
        rels = gh_releases(repo, prefix=prefix, per_page=100)
        latest = rels[0][0].replace(prefix, "") if rels else "?"
        date = rels[0][1] if rels else "?"
        rows.append(Pin(name, pinned, latest, date, status(pinned, latest)))

    rels = gh_releases("aws/karpenter-provider-aws", prefix="v", per_page=5)
    latest = rels[0][0].lstrip("v") if rels else "?"
    rows.append(Pin("karpenter chart/controller", "1.12.0", latest, rels[0][1] if rels else "?", status("1.12.0", latest)))

    try:
        v, d = helm_chart_latest("https://aws.github.io/eks-charts", "aws-load-balancer-controller")
        rows.append(Pin("aws-load-balancer-controller chart", "3.2.2", v, d, status("3.2.2", v)))
    except Exception:
        rows.append(Pin("aws-load-balancer-controller chart", "3.2.2", "ERR", "?", "ERR"))

    # Helm charts published via classic (index.yaml) repos. Pins mirror
    # terraform/versions.tf local.charts; helm_chart_latest reads the repo
    # index so a newer published chart shows as BUMP. (Karpenter is OCI and is
    # checked via gh releases above, not an index.yaml repo.)
    index_charts = [
        ("mimir-distributed chart", "https://grafana.github.io/helm-charts", "mimir-distributed", "6.0.6"),
        ("loki chart", "https://grafana.github.io/helm-charts", "loki", "7.0.0"),
        ("tempo-distributed chart", "https://grafana.github.io/helm-charts", "tempo-distributed", "1.61.3"),
        ("grafana chart", "https://grafana.github.io/helm-charts", "grafana", "10.5.15"),
        ("alloy chart", "https://grafana.github.io/helm-charts", "alloy", "1.8.0"),
        ("jenkins chart", "https://charts.jenkins.io", "jenkins", "5.9.18"),
        ("cloudnative-pg chart", "https://cloudnative-pg.github.io/charts", "cloudnative-pg", "0.28.2"),
    ]
    for name, repo, chart, pinned in index_charts:
        try:
            v, d = helm_chart_latest(repo, chart)
            rows.append(Pin(name, pinned, v, d, status(pinned, v)))
        except Exception as e:
            rows.append(Pin(name, pinned, "ERR", "?", f"ERR:{type(e).__name__}"))

    rels = gh_releases("opentofu/opentofu", prefix="v", per_page=10)
    stable = [r for r in rels if all(t not in r[0] for t in ("alpha", "beta", "rc"))]
    latest = stable[0][0].lstrip("v") if stable else "?"
    rows.append(Pin("OpenTofu", "1.11.6", latest, stable[0][1] if stable else "?", status("1.11.6", latest)))

    extra_tools = [
        ("tflint", "terraform-linters/tflint", "v", "0.55.1"),
        ("actionlint", "rhysd/actionlint", "v", "TBD"),
        ("zizmor", "woodruffw/zizmor", "v", "TBD"),
        ("kubeconform", "yannh/kubeconform", "v", "TBD"),
        ("aws-cli v2", "aws/aws-cli", "", "TBD"),
        ("argo-cd core", "argoproj/argo-cd", "v", "(via chart)"),
        ("external-secrets", "external-secrets/external-secrets", "v", "TBD"),
        ("trivy", "aquasecurity/trivy", "v", "TBD"),
        ("just", "casey/just", "", "TBD"),
        ("yamllint", "adrienverge/yamllint", "v", "TBD"),
        ("pre-commit", "pre-commit/pre-commit", "v", "TBD"),
    ]
    for name, repo, prefix, pinned in extra_tools:
        try:
            rels = gh_releases(repo, prefix=prefix or None, per_page=5)
            stable = [r for r in rels if all(t not in r[0].lower() for t in ("alpha", "beta", "rc", "pre"))]
            if not stable:
                stable = rels
            latest = stable[0][0].lstrip("v") if stable else "?"
            date = stable[0][1] if stable else "?"
            rows.append(Pin(name, pinned, latest, date, "INFO" if pinned == "(via chart)" else status(pinned, latest)))
        except Exception as e:
            rows.append(Pin(name, pinned, "ERR", "?", f"ERR:{type(e).__name__}"))

    for p in rows:
        print(f"{p.name:<42} {p.pinned:<14} {p.latest:<14} {p.date:<12} {p.status}")

    print()
    print("=== EKS supported versions (aws eks describe-cluster-versions) ===")
    try:
        out = subprocess.check_output(
            ["aws", "eks", "describe-cluster-versions", "--region", "us-east-1",
             "--profile", "percona-dev-admin", "--output", "json"],
            stderr=subprocess.DEVNULL, timeout=30,
        )
        data = json.loads(out)
        print(f"{'K8s':<8} {'Status':<22} {'Patch':<10} {'EOS-Standard':<15} {'EOS-Extended':<15} Default")
        for cv in data.get("clusterVersions", []):
            print(
                f"{cv['clusterVersion']:<8} {cv['versionStatus']:<22} "
                f"{cv['kubernetesPatchVersion']:<10} {cv['endOfStandardSupportDate'][:10]:<15} "
                f"{cv['endOfExtendedSupportDate'][:10]:<15} {cv.get('defaultVersion', False)}"
            )
    except Exception as e:
        print(f"AWS CLI EKS query failed: {e}")

    return 0


if __name__ == "__main__":
    sys.exit(main())
