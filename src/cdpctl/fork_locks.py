"""Recorded-pin auto-bump for the Percona Jenkins fork plugins.

Reads images/jenkins/percona-plugins.lock.json and, for every plugin entry:
  1. derives the GitHub owner/repo from that entry's existing release `url`
     (the lock is the single source of truth; no hard-coded repo map)
  2. lists the repo's releases (forks are public, so no token is needed to
     LIST; GH_TOKEN is used only to raise the API rate limit when present),
     drops drafts, drops prereleases unless ALLOW_PRERELEASE=1, and keeps only
     tags carrying a `.percona.` fork version (an upstream-synced tag is
     ignored)
  3. picks the highest version, numeric-aware (.9 < .10 < .26)
  4. only when that is strictly newer than the locked version: downloads the
     single `<id>-<ver>.hpi` release asset (the `.hpi.sha256` sidecar is NOT a
     second match), integrity-checks the zip, verifies the embedded MANIFEST
     `Short-Name`==id and `Plugin-Version`==ver, computes sha256 (cross-checked
     against the published `.hpi.sha256` sidecar when present), then rewrites
     that lock entry (version/filename/url/sha256).

It NEVER edits the Dockerfile, fetch-hpis.sh, or the values-base image tag: the
build stays pinned+sha-verified+smoke-asserted and the DEPLOY stays manual
(ADR 0025). The CI wrapper (.github/workflows/refresh-fork-locks.yml) re-runs
fetch + build + smoke against the rewritten lock, then opens a PR; CODEOWNERS
approves. No auto-merge, no auto-bump of the deployed image tag.

Usage:
  cdpctl fork-locks            rewrite the lock in place (if newer)
  cdpctl fork-locks --check    report only; exit 3 if a bump exists
  ALLOW_PRERELEASE=1 cdpctl fork-locks   include prereleases
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import sys
import tempfile
import time
import urllib.error
import urllib.request
import zipfile
from typing import NoReturn

from cdpctl import _stage
from cdpctl._repo import repo_root


def _fail(msg: str) -> NoReturn:
    print(f"FAIL: {msg}", file=sys.stderr)
    raise SystemExit(1)


def version_key(v: str) -> list[tuple[int, int] | tuple[int, str]]:
    """GNU `sort -V`-equivalent ordering key for fork tags like 103.percona.28."""
    return [
        (0, int(chunk)) if chunk.isdigit() else (1, chunk)
        for chunk in re.findall(r"\d+|[^\d.]+", v)
    ]


def _api(url: str):
    headers = {"Accept": "application/vnd.github+json"}
    token = os.environ.get("GH_TOKEN")
    if token:
        headers["Authorization"] = f"Bearer {token}"
    req = urllib.request.Request(url, headers=headers)
    with urllib.request.urlopen(req, timeout=30) as r:
        return json.load(r)


def _download(url: str, dest: str) -> None:
    last: Exception | None = None
    for _ in range(3):
        try:
            with urllib.request.urlopen(url, timeout=120) as r, open(dest, "wb") as f:
                while chunk := r.read(1 << 16):
                    f.write(chunk)
            return
        except (urllib.error.URLError, OSError) as e:
            last = e
            time.sleep(2)
    raise SystemExit(f"FAIL: download failed after retries: {url} ({last})")


def _manifest_field(manifest: str, field: str) -> str:
    for line in manifest.replace("\r", "").splitlines():
        if line.startswith(f"{field}: "):
            return line.split(": ", 1)[1]
    return ""


def latest_eligible(releases: list[dict], allow_prerelease: bool) -> str:
    """Highest `.percona.` tag (v-stripped), drafts and prereleases filtered."""
    tags = [
        str(rel["tag_name"]).removeprefix("v")
        for rel in releases
        if not rel.get("draft")
        and (allow_prerelease or not rel.get("prerelease"))
        and ".percona." in str(rel.get("tag_name", ""))
    ]
    tags.sort(key=version_key)
    return tags[-1] if tags else ""


def _verify_hpi(tmp: str, plugin_id: str, version: str) -> str:
    """Zip-integrity + MANIFEST identity checks; returns the sha256 hex digest."""
    try:
        with zipfile.ZipFile(tmp) as z:
            if z.testzip() is not None:
                _fail(f"{plugin_id} {version} corrupt HPI (unzip -t failed)")
            manifest = z.read("META-INF/MANIFEST.MF").decode("utf-8", "replace")
    except zipfile.BadZipFile:
        _fail(f"{plugin_id} {version} corrupt HPI (unzip -t failed)")
    m_id = _manifest_field(manifest, "Short-Name")
    m_ver = _manifest_field(manifest, "Plugin-Version")
    if m_id != plugin_id:
        _fail(f"{plugin_id} MANIFEST Short-Name='{m_id}' (expected {plugin_id})")
    if m_ver != version:
        _fail(f"{plugin_id} MANIFEST Plugin-Version='{m_ver}' (expected {version})")
    h = hashlib.sha256()
    with open(tmp, "rb") as f:
        while chunk := f.read(1 << 16):
            h.update(chunk)
    return h.hexdigest()


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="cdpctl fork-locks", description=__doc__)
    ap.add_argument("--check", action="store_true", help="report only; exit 3 if a bump exists")
    _stage.add_output_flags(ap)
    args = ap.parse_args(argv)
    mode = _stage.output_mode(args)
    table: list[list[str]] = []

    lock_path = os.path.join(repo_root(), "images/jenkins/percona-plugins.lock.json")
    allow_prerelease = os.environ.get("ALLOW_PRERELEASE", "0") == "1"
    if not os.path.isfile(lock_path):
        print(f"lock not found: {lock_path}", file=sys.stderr)
        return 1
    with open(lock_path, encoding="utf-8") as f:
        lock = json.load(f)
    plugins = lock.get("plugins", [])
    if not plugins:
        print("lock has zero plugins", file=sys.stderr)
        return 1

    changed = False
    for entry in plugins:
        plugin_id, cur, url = entry["id"], entry["version"], entry["url"]

        m = re.match(r"^https://github\.com/([^/]+/[^/]+)/releases/.*", url)
        if not m:
            _fail(f"{plugin_id} cannot parse owner/repo from url: {url}")
        repo = m.group(1)

        releases = _api(f"https://api.github.com/repos/{repo}/releases?per_page=100")
        latest = latest_eligible(releases, allow_prerelease)
        if not latest:
            _fail(f"{plugin_id} ({repo}) no eligible (.percona.) release found")

        # Strict-newer gate: skip equality AND any case where the lock is already
        # >= the newest published release (manual pin ahead must not downgrade).
        if latest == cur or max(cur, latest, key=version_key) != latest:
            table.append([plugin_id, cur, latest, "UP-TO-DATE"])
            continue

        table.append([plugin_id, cur, latest, "BUMP"])
        changed = True
        if args.check:
            continue

        tag = f"v{latest}"
        fname = f"{plugin_id}-{latest}.hpi"
        rel = next((r for r in releases if r["tag_name"] == tag), {"assets": []})
        hpi_assets = [
            a for a in rel["assets"] if re.match(rf"^{re.escape(plugin_id)}-.*\.hpi$", a["name"])
        ]
        if len(hpi_assets) != 1:
            _fail(f"{plugin_id} {tag} expected exactly one <id>-*.hpi asset, got {len(hpi_assets)}")
        asset = next((a for a in rel["assets"] if a["name"] == fname), None)
        if asset is None:
            _fail(f"{plugin_id} {tag} missing asset {fname}")
        with tempfile.NamedTemporaryFile(delete=False) as tf:
            tmp = tf.name
        try:
            _download(asset["browser_download_url"], tmp)
            sha = _verify_hpi(tmp, plugin_id, latest)
            # Defense in depth: a published <fname>.sha256 sidecar must agree.
            side = next((a for a in rel["assets"] if a["name"] == f"{fname}.sha256"), None)
            if side is not None:
                with urllib.request.urlopen(side["browser_download_url"], timeout=30) as r:
                    want = r.read().decode().split()[0] if r else ""
                if want and want != sha:
                    print(
                        f"FAIL: {plugin_id} {latest} sha256 disagrees with published sidecar",
                        file=sys.stderr,
                    )
                    print(f"  computed {sha}", file=sys.stderr)
                    print(f"  sidecar  {want}", file=sys.stderr)
                    return 1
        finally:
            os.unlink(tmp)

        entry["version"] = latest
        entry["filename"] = fname
        entry["url"] = f"https://github.com/{repo}/releases/download/{tag}/{fname}"
        entry["sha256"] = sha
        with open(lock_path, "w", encoding="utf-8") as f:
            json.dump(lock, f, indent=2)
            f.write("\n")
        if mode == "human":
            print(f"  locked {plugin_id} {latest} sha256={sha}")

    _stage.emit_rows(
        ["plugin", "locked", "latest", "status"],
        table,
        mode,
        json_payload={
            "check_only": args.check,
            "changed": changed,
            "plugins": [
                {"plugin": p_, "locked": lo, "latest": la, "status": st_}
                for p_, lo, la, st_ in table
            ],
        },
    )
    if args.check:
        if changed:
            if mode == "human":
                print("\nbump available (--check)")
            return 3
        return 0
    if mode == "human":
        print("\nlock updated" if changed else "\nno changes")
    return 0


if __name__ == "__main__":
    sys.exit(main())
