"""Jenkins REST API fetcher -- stdlib replacement for the Rust `jenkins` CLI.

Used inside Jenkins pipeline jobs where the Rust binary is not available.
Uses urllib.request for HTTP and produces the same dict shapes that
build_to_json.py expects from jenkins_fetcher.py.
"""

from __future__ import annotations

import base64
import json
import os
import re
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen


class JenkinsFetchError(RuntimeError):
    """Raised on HTTP or parse failures."""


def cli_version() -> str:
    return "rest-api/1.0"


# --- HTTP helpers ------------------------------------------------------------

def _auth_header(username: str | None, token: str | None) -> dict[str, str]:
    if username and token:
        cred = base64.b64encode(f"{username}:{token}".encode()).decode()
        return {"Authorization": f"Basic {cred}"}
    return {}


def _get_json(url: str, username: str | None = None, token: str | None = None) -> dict | list:
    headers = {"Accept": "application/json", **_auth_header(username, token)}
    req = Request(url, headers=headers)
    try:
        with urlopen(req, timeout=120) as resp:
            return json.loads(resp.read())
    except HTTPError as e:
        raise JenkinsFetchError(f"HTTP {e.code} for {url}: {e.reason}") from e
    except (URLError, OSError) as e:
        raise JenkinsFetchError(f"Request failed for {url}: {e}") from e
    except json.JSONDecodeError as e:
        raise JenkinsFetchError(f"Non-JSON response from {url}: {e}") from e


def _download_file(url: str, dest: Path,
                   username: str | None = None, token: str | None = None) -> None:
    headers = _auth_header(username, token)
    req = Request(url, headers=headers)
    try:
        with urlopen(req, timeout=300) as resp:
            dest.parent.mkdir(parents=True, exist_ok=True)
            dest.write_bytes(resp.read())
    except (HTTPError, URLError, OSError) as e:
        raise JenkinsFetchError(f"Download failed for {url}: {e}") from e


# --- public API (same contract as jenkins_fetcher.py) ------------------------

def fetch_history(
    base_url: str,
    job: str,
    limit: int,
    username: str | None = None,
    token: str | None = None,
) -> list[dict]:
    """Return list of build dicts: [{number, result, duration, timestamp}, ...].

    ``base_url`` is the Jenkins root, e.g. ``https://ps80.cd.percona.com``.
    """
    tree = "builds[number,result,duration,timestamp]"
    url = f"{base_url.rstrip('/')}/job/{job}/api/json?tree={tree}{{0,{limit}}}"
    payload = _get_json(url, username, token)
    builds = payload.get("builds", []) if isinstance(payload, dict) else []
    # Normalize to match Rust CLI shape: rename duration -> duration_ms is not
    # needed here because backfill.py only uses "number" and "result" from history.
    # But keep the raw fields for forward-compat.
    return builds


def fetch_detail(
    base_url: str,
    job: str,
    build_number: int,
    username: str | None = None,
    token: str | None = None,
) -> dict:
    """Return normalized build detail matching the shape build_to_json.py expects."""
    url = f"{base_url.rstrip('/')}/job/{job}/{build_number}/api/json"
    payload = _get_json(url, username, token)
    if not isinstance(payload, dict):
        raise JenkinsFetchError(f"Unexpected response type: {type(payload).__name__}")
    return _normalize_detail(payload)


def download_junit_xmls(
    base_url: str,
    job: str,
    build_number: int,
    dest_dir: Path,
    username: str | None = None,
    token: str | None = None,
) -> list[Path]:
    """Download junit_*.xml artifacts to dest_dir; return sorted list of paths."""
    dest_dir.mkdir(parents=True, exist_ok=True)

    # List artifacts via API.
    url = f"{base_url.rstrip('/')}/job/{job}/{build_number}/api/json?tree=artifacts[fileName,relativePath]"
    payload = _get_json(url, username, token)
    artifacts = payload.get("artifacts", []) if isinstance(payload, dict) else []

    downloaded: list[Path] = []
    for art in artifacts:
        fname = art.get("fileName", "")
        rel_path = art.get("relativePath", "")
        if not fname.startswith("junit_") or not fname.endswith(".xml"):
            continue
        artifact_url = f"{base_url.rstrip('/')}/job/{job}/{build_number}/artifact/{rel_path}"
        local_path = dest_dir / fname
        _download_file(artifact_url, local_path, username, token)
        downloaded.append(local_path)

    return sorted(downloaded)


# --- detail normalization ----------------------------------------------------

_CAUSE_KIND_MAP = {
    "hudson.model.Cause$UserIdCause": "Manual",
    "hudson.model.Cause$RemoteCause": "Remote",
    "hudson.model.Cause$UpstreamCause": "Upstream",
    "hudson.triggers.TimerTrigger$TimerTriggerCause": "Timer",
    "hudson.triggers.SCMTrigger$SCMTriggerCause": "SCM",
    "org.jenkinsci.plugins.gwt.GenericCause": "Webhook",
}


def _normalize_detail(payload: dict) -> dict:
    """Map raw Jenkins /api/json response to the dict shape build_to_json.py expects."""
    actions = payload.get("actions") or []

    # --- parameters ---
    parameters: list[dict[str, str]] = []
    for action in actions:
        if not isinstance(action, dict):
            continue
        for p in action.get("parameters", []):
            if isinstance(p, dict) and "name" in p:
                parameters.append({"name": p["name"], "value": p.get("value") or ""})

    # --- cause ---
    cause = {"kind": "Unknown", "user": None, "description": ""}
    for action in actions:
        if not isinstance(action, dict):
            continue
        for c in action.get("causes", []):
            if not isinstance(c, dict):
                continue
            cls = c.get("_class", "")
            cause["kind"] = _CAUSE_KIND_MAP.get(cls, cls.rsplit("$", 1)[-1] if "$" in cls else "Unknown")
            cause["user"] = c.get("userName") or c.get("userId")
            cause["description"] = c.get("shortDescription") or ""
            break
        if cause["kind"] != "Unknown":
            break

    # --- source (derived from parameters) ---
    params_dict = {p["name"]: p["value"] for p in parameters}
    git_repo = params_dict.get("GIT_REPO", "")
    branch = params_dict.get("BRANCH", "")
    fork = None
    if git_repo:
        stripped = git_repo.rstrip("/").removesuffix(".git")
        parts = stripped.split("/")
        if len(parts) >= 2:
            fork = parts[-2]
    source = {"repo": git_repo, "fork": fork, "branch": branch, "sha": None}

    # --- scm (from BuildData actions) ---
    scm: list[dict[str, str]] = []
    for action in actions:
        if not isinstance(action, dict):
            continue
        cls = action.get("_class", "")
        if "BuildData" not in cls:
            continue
        remotes = action.get("remoteUrls") or []
        last_rev = action.get("lastBuiltRevision") or {}
        sha = last_rev.get("SHA1", "")
        branches = last_rev.get("branch") or []
        branch_name = branches[0].get("name", "") if branches else ""
        for remote in remotes:
            scm.append({"remote": remote, "branch": branch_name, "sha": sha})

    return {
        "build": payload.get("number"),
        "url": payload.get("url", ""),
        "timestamp_ms": payload.get("timestamp"),
        "duration_ms": payload.get("duration"),
        "result": payload.get("result") or "UNKNOWN",
        "display_name": payload.get("displayName") or f"#{payload.get('number', '?')}",
        "parameters": parameters,
        "source": source,
        "cause": cause,
        "scm": scm,
    }
