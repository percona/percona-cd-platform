#!/usr/bin/env python3
"""Terraform conventions gate (see terraform/CLAUDE.md).

Asserts, fail-closed:
  1. Every terraform/*.tf and terraform/modules/*/main.tf starts with an
     `# Owner: <value>` banner from the allowed set.
  2. Every master-<inst>.tf banner matches the instance -> team map, and
     every `team = "<value>"` module argument in master-<inst>.tf matches the
     per-instance team TAG map (cloud tags team=cloud-cd so the cloud team's
     orphan-cleanup lambdas never match the master or its workers; the Owner
     banner stays `cloud`).
  3. No `Copyright` line in any .tf (Percona's own IaC carries no headers;
     copyright headers belong to product source via .licenserc.yaml).
  4. No `CLAUDE.md` reference in .tf text (gotcha numbers renumber; cite an
     ADR or docs/ anchor instead).
  5. No Jira ticket IDs (PS-/PKG-/PXB-/PG-NNNN) in .tf COMMENT text, for the
     `#` and `//` line-comment forms. Functional arguments (e.g.
     `tickets = "PS-11179"`) and quoted strings are exempt: only text after a
     comment marker is scanned. Known accepted gaps: `/* */` block comments
     (untracked; ARN strings like ":instance/*" would false-open a block, and
     tofu fmt keeps the repo on line comments) and a `#`/`//` inside a quoted
     string (false-positive risk).

Credential-free, stdlib-only. Run from the repo root:
  python3 scripts/check_conventions.py
"""

from __future__ import annotations

import pathlib
import re
import sys

REPO = pathlib.Path(__file__).resolve().parents[1]
TF_ROOT = REPO / "terraform"

OWNERS = {
    "platform",
    "mysql",
    "xtrabackup",
    "pxc",
    "mongodb",
    "postgresql",
    "release",
    "cloud",
    "pmm",
}

# Instance -> owning team. Each product team is served by its own Jenkins
# instance(s); pmm is listed for when master-pmm.tf lands.
INSTANCE_TEAM = {
    "ps3": "mysql",
    "ps57": "mysql",
    "ps80": "mysql",
    "pxb": "xtrabackup",
    "pxc": "pxc",
    "psmdb": "mongodb",
    "pg": "postgresql",
    "rel": "release",
    "cloud": "cloud",
    "pmm": "pmm",
}

# Expected `team = "<value>"` module argument per instance: INSTANCE_TEAM,
# except cloud. The cloud team's hourly orphan-cleanup lambdas terminate any
# running instance tagged team=cloud that lacks a TTL tag, so cloud resources
# tag team=cloud-cd instead (docs/runbooks/cleanup-reapers.md, foreign
# reapers). Only the tag value diverges; the Owner banner stays "cloud".
INSTANCE_TEAM_TAG = {**INSTANCE_TEAM, "cloud": "cloud-cd"}

BANNER_RE = re.compile(r"^# Owner: ([a-z0-9-]+)$")
TICKET_RE = re.compile(r"\b(?:PS|PKG|PXB|PG)-\d+\b")
# Anchored at line start so `team=cloud` inside comment prose never matches;
# only real module-argument assignment lines do.
TEAM_ARG_RE = re.compile(r'^\s*team\s*=\s*"([a-z0-9-]+)"')


def banner_files() -> list[pathlib.Path]:
    return sorted(TF_ROOT.glob("*.tf")) + sorted(TF_ROOT.glob("modules/*/main.tf"))


def all_tf_files() -> list[pathlib.Path]:
    return [
        p
        for p in sorted(TF_ROOT.rglob("*.tf"))
        if ".terraform" not in p.parts and "_oneoff" not in p.parts
    ]


def main() -> int:
    errors: list[str] = []

    for path in banner_files():
        rel = path.relative_to(REPO)
        first = path.read_text().splitlines()[0] if path.read_text() else ""
        m = BANNER_RE.match(first)
        if not m:
            errors.append(f"{rel}:1  missing `# Owner: <team>` banner (got: {first[:60]!r})")
            continue
        owner = m.group(1)
        if owner not in OWNERS:
            errors.append(f"{rel}:1  unknown owner {owner!r} (allowed: {', '.join(sorted(OWNERS))})")
        inst_match = re.match(r"master-([a-z0-9]+)\.tf$", path.name)
        if inst_match:
            inst = inst_match.group(1)
            want = INSTANCE_TEAM.get(inst)
            if want is None:
                errors.append(f"{rel}:1  instance {inst!r} not in INSTANCE_TEAM map")
            elif owner != want:
                errors.append(f"{rel}:1  owner {owner!r} but instance {inst!r} belongs to {want!r}")
            if want is not None:
                want_tag = INSTANCE_TEAM_TAG[inst]
                team_args = [
                    (n, m.group(1))
                    for n, line in enumerate(path.read_text().splitlines(), start=1)
                    if (m := TEAM_ARG_RE.match(line))
                ]
                if not team_args:
                    errors.append(f'{rel}  no `team = "..."` module argument (expected {want_tag!r})')
                for n, got in team_args:
                    if got != want_tag:
                        errors.append(f"{rel}:{n}  team {got!r} but instance {inst!r} must tag team={want_tag!r}")

    for path in all_tf_files():
        rel = path.relative_to(REPO)
        for n, line in enumerate(path.read_text().splitlines(), start=1):
            if "Copyright" in line:
                errors.append(f"{rel}:{n}  `Copyright` line (no copyright headers in IaC)")
            if "CLAUDE.md" in line:
                errors.append(f"{rel}:{n}  `CLAUDE.md` reference (cite an ADR or docs/ anchor)")

            # Comment text = everything after the earliest '#' or '//' marker.
            # '/* ... */' block comments are deliberately NOT tracked: IAM ARN
            # strings ("...:instance/*") would false-open a block, and tofu fmt
            # (a CI gate) keeps this repo on line comments anyway.
            markers = [m for m in (line.find("#"), line.find("//")) if m != -1]
            if markers:
                hit = TICKET_RE.search(line[min(markers):])
                if hit:
                    errors.append(f"{rel}:{n}  ticket ID {hit.group(0)} in comment (keep IDs out of .tf comments)")

    if errors:
        print(f"check_conventions: {len(errors)} violation(s)")
        for e in errors:
            print(f"  {e}")
        return 1

    print(f"check_conventions: OK ({len(banner_files())} banners, {len(all_tf_files())} files scanned)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
