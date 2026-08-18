#!/usr/bin/env python3
"""Resolve a minimal, core-compatible plugins.txt change set.

Three failure modes this exists to avoid, all hit for real while writing it:

1. Bumping only the target plugins leaves their pinned dependencies stale, and
   jenkins-plugin-cli rejects the manifest with
   AggregatePluginPrerequisitesNotMetException.
2. Pinning every id to the catalog's newest is consistent but moves ~128
   plugins, dragging in unrelated major breaks (job-dsl 1.93 -> 3732,
   pipeline-utility-steps 2.20 -> 3.810, okhttp-api 4 -> 5).
3. The catalog's newest version of a plugin often requires a NEWER CORE than
   the one we run (workflow-job 1590 needs 2.556, we are on 2.541.3), so
   "latest" is not installable at all.

So: for each plugin that must move, pick the newest version whose requiredCore
is satisfied by OUR core, then propagate that exact version's dependency
minimums, transitively, over the pins only. Anything that cannot be satisfied
without a newer core is reported as CORE-BLOCKED rather than silently skipped,
because that is a planning fact: those fixes need the core upgrade first.

Reads updates.jenkins.io plugin-versions.json (every version, with
requiredCore and per-version dependencies).

Usage:
  resolve-plugins.py --manifest plugins.txt --versions plugin-versions.json \
      --core 2.541.3 --targets a,b,c [--apply] [--report out.txt]
"""
import argparse
import json
import re
import sys
from pathlib import Path

# Baked from percona-plugins.lock.json, never pinned in plugins.txt.
FORKS = {"ec2", "hetzner-cloud"}


def split_version(version):
    """Tokenize a Jenkins version. Non-numeric tokens sort BEFORE numeric ones.

    Jenkins treats workflow-job 1571.1580.v18e46842c125 as NEWER than
    1571.vb_423c255d6d9: the second token is a build number in one and a commit
    hash in the other. Getting this backwards under-resolves the set and
    plugin-cli rejects the result.
    """
    tokens = []
    for chunk in re.split(r"[.\-]", version):
        if chunk.isdigit():
            tokens.append((1, int(chunk), ""))
        else:
            leading = re.match(r"^(\d+)(.*)$", chunk)
            if leading:
                tokens.append((1, int(leading.group(1)), leading.group(2)))
            else:
                tokens.append((0, -1, chunk))
    return tokens


def version_lt(left, right):
    if left == right:
        return False
    left_tokens, right_tokens = split_version(left), split_version(right)
    for a, b in zip(left_tokens, right_tokens):
        if a != b:
            return a < b
    return len(left_tokens) < len(right_tokens)


def self_test():
    """Orderings jenkins-plugin-cli has independently confirmed."""
    pairs = [
        ("1571.vb_423c255d6d9", "1571.1580.v18e46842c125"),
        ("710.v3e456cc85233", "724.v538c2362b_dfb_"),
        ("6.5.0", "6.6.1"),
        ("427", "429"),
        ("3.2.9", "3.3"),
        ("4.2.3.539.v8fedff2a_81c3", "4.4.0.554.ve1d40b_c02e16"),
        ("2.30.1.82-277.v70ca_0b_877184", "2.30.1.84-291.v9f17b_21896e2"),
        ("1.93", "3732.v9a_c49a_61a_313"),
        ("2.541.3", "2.556"),
        ("2.541.3", "2.568.2"),
    ]
    bad = []
    for older, newer in pairs:
        if not version_lt(older, newer):
            bad.append(f"expected {older} < {newer}")
        if version_lt(newer, older):
            bad.append(f"expected NOT {newer} < {older}")
    if version_lt("6.6.1", "6.6.1"):
        bad.append("equal versions must not compare less-than")
    if bad:
        for line in bad:
            print("SELF-TEST FAIL: " + line, file=sys.stderr)
        raise AssertionError(f"comparator self-test failed ({len(bad)} cases)")
    print(f"comparator self-test: {len(pairs)} orderings OK")


def core_ok(required_core, core):
    """True when a plugin needing `required_core` runs on `core`."""
    if not required_core:
        return True
    return not version_lt(core, required_core)


def newest_compatible(history, core, at_least=None):
    """Newest version runnable on `core`, optionally >= at_least."""
    best = None
    for version, entry in history.items():
        if not core_ok(entry.get("requiredCore"), core):
            continue
        if at_least and version_lt(version, at_least):
            continue
        if best is None or version_lt(best, version):
            best = version
    return best


def resolve(pins, versions, core, targets):
    desired = dict(pins)
    reason = {}
    blocked = {}

    queue = []

    def move(plugin_id, at_least, why):
        """Move a pinned plugin to the newest core-compatible version >= at_least."""
        history = versions.get(plugin_id)
        if not history:
            blocked[plugin_id] = f"absent from the update center ({why})"
            return
        pick = newest_compatible(history, core, at_least)
        if pick is None:
            newest_any = None
            for version in history:
                if newest_any is None or version_lt(newest_any, version):
                    newest_any = version
            needs = history[newest_any].get("requiredCore", "?")
            blocked[plugin_id] = (
                f"CORE-BLOCKED: {why}: needs >={at_least or 'newest'}, but every such "
                f"version wants core >{core} (newest {newest_any} needs {needs})"
            )
            return
        if pick == desired[plugin_id]:
            reason.setdefault(plugin_id, f"already at {pick} ({why})")
            return
        if version_lt(pick, desired[plugin_id]):
            reason.setdefault(plugin_id, f"pin {desired[plugin_id]} already newer than {pick}")
            return
        desired[plugin_id] = pick
        reason[plugin_id] = why + f" -> {pick}"
        queue.append(plugin_id)

    for plugin_id in targets:
        if plugin_id in FORKS:
            reason[plugin_id] = "SKIPPED: fork, baked from the lock file"
            continue
        if plugin_id not in pins:
            reason[plugin_id] = "SKIPPED: not pinned in the manifest"
            continue
        move(plugin_id, None, f"TARGET (was {pins[plugin_id]})")

    guard = 0
    while queue:
        guard += 1
        assert guard < 20000, "dependency fixpoint did not converge"
        plugin_id = queue.pop()
        entry = versions[plugin_id][desired[plugin_id]]
        for dep in entry.get("dependencies", []):
            dep_id, required = dep["name"], dep.get("version")
            if dep_id in FORKS or dep_id not in desired or not required:
                continue  # unpinned deps are plugin-cli's job
            if not version_lt(desired[dep_id], required):
                continue
            move(dep_id, required, f"required by {plugin_id} (>={required})")

    return desired, reason, blocked


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--manifest", required=True, type=Path)
    parser.add_argument("--versions", required=True, type=Path)
    parser.add_argument("--core", required=True)
    parser.add_argument("--targets", required=True)
    parser.add_argument("--apply", action="store_true")
    parser.add_argument("--report", type=Path)
    args = parser.parse_args()

    self_test()

    versions = json.loads(args.versions.read_text())["plugins"]
    assert len(versions) > 1500, f"version index too small: {len(versions)}"

    lines = args.manifest.read_text().splitlines()
    pins = {}
    for line in lines:
        if not line.startswith("#") and ":" in line:
            plugin_id, version = line.split(":", 1)
            pins[plugin_id] = version
    assert len(pins) > 200, f"manifest too small: {len(pins)}"

    targets = [t.strip() for t in args.targets.split(",") if t.strip()]
    assert targets, "no targets given"

    desired, reason, blocked = resolve(pins, versions, args.core, targets)
    moved = {i: (pins[i], v) for i, v in desired.items() if pins[i] != v}

    out = []
    out.append(f"core={args.core} targets={len(targets)} moved={len(moved)} "
               f"blocked={len(blocked)} untouched={len(pins) - len(moved)}")
    out.append("")
    out.append("--- MOVED ---")
    for plugin_id in sorted(moved):
        old, new = moved[plugin_id]
        out.append(f"{plugin_id}: {old} -> {new}   [{reason.get(plugin_id, 'transitive')}]")
    out.append("")
    out.append("--- BLOCKED (needs a newer core, or absent upstream) ---")
    for plugin_id in sorted(blocked):
        out.append(f"{plugin_id}: {blocked[plugin_id]}")
    out.append("")
    out.append("--- TARGETS NOT MOVED ---")
    for plugin_id in targets:
        if plugin_id not in moved and plugin_id not in blocked:
            out.append(f"{plugin_id}: {reason.get(plugin_id, 'no reason recorded')}")

    text = "\n".join(out)
    print(text)
    if args.report:
        args.report.write_text(text + "\n")

    if args.apply:
        assert moved, "nothing moved; refusing to rewrite the manifest"
        rewritten = []
        for line in lines:
            if line.startswith("#") or ":" not in line:
                rewritten.append(line)
                continue
            plugin_id, _ = line.split(":", 1)
            rewritten.append(f"{plugin_id}:{desired[plugin_id]}")
        args.manifest.write_text("\n".join(rewritten) + "\n")
        print(f"\napplied {len(moved)} pin changes to {args.manifest}")

    return 0


if __name__ == "__main__":
    sys.exit(main())
