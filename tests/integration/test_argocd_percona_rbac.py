# /// script
# requires-python = ">=3.11"
# dependencies = ["pytest>=8"]
# ///
"""
Live audit: can the `percona` Authentik group see ArgoCD secrets?

The `percona` FreeIPA/Duo group is mapped in the live argocd-rbac-cm to ArgoCD's
built-in role:readonly (g, percona, role:readonly). This drives ArgoCD's OWN RBAC
evaluator (`argocd admin settings rbac can`) against the LIVE policy plus the
built-in role definitions, so the verdicts are real enforcement decisions.

This is a LIVE integration test (needs the argocd CLI + a kube context), run on
demand via `just audit-argocd-rbac`, not in PR CI. It skips cleanly when the
tools or cluster are unavailable.

What it proves about secrets:
  * percona CAN `get applications`  -> can SEE Secret OBJECTS in an app's tree
    (names/health), but argocd-server redacts the values to ******** for all users.
  * percona CANNOT `create exec`    -> no pod shell to print decoded values.
  * percona CANNOT sync/create/update/delete -> read-only, cannot mutate.
Net: percona can observe that secrets exist, but has no path to their plaintext.

No secret values are read or printed.
"""
from __future__ import annotations

import os
import shutil
import subprocess
import sys
import tempfile

import pytest

ARGOCD = os.environ.get("ARGOCD_BIN", "argocd")
KUBECTL = os.environ.get("KUBECTL_BIN", "kubectl")
RBAC_NS = os.environ.get("ARGOCD_NAMESPACE", "argocd")


@pytest.fixture(scope="session", autouse=True)
def _require_tools():
    for cmd in (ARGOCD, KUBECTL):
        if shutil.which(cmd) is None:
            pytest.skip(f"{cmd} not on PATH; live ArgoCD RBAC audit skipped")


def _pull_live_policy() -> str:
    """Fetch policy.csv from the live argocd-rbac-cm into a temp file."""
    out = subprocess.run(
        [KUBECTL, "-n", RBAC_NS, "get", "cm", "argocd-rbac-cm",
         "-o", r"jsonpath={.data.policy\.csv}"],
        capture_output=True, text=True,
    )
    if out.returncode != 0 or not out.stdout.strip():
        pytest.skip(f"cannot read live argocd-rbac-cm: {out.stderr.strip() or 'empty'}")
    fd, path = tempfile.mkstemp(prefix="argocd_policy_", suffix=".csv")
    with os.fdopen(fd, "w") as fh:
        fh.write(out.stdout)
    return path


@pytest.fixture(scope="session")
def policy_file() -> str:
    return os.environ.get("ARGOCD_POLICY_FILE") or _pull_live_policy()


@pytest.fixture(scope="session")
def policy_text(policy_file: str) -> str:
    with open(policy_file) as fh:
        return fh.read()


def rbac_can(subject: str, action: str, resource: str, policy_file: str,
             scope: str = "*/*") -> bool | None:
    """Return True/False from the ArgoCD RBAC evaluator, or None if no verdict.

    policy.default is empty in this cluster, so --default-role '' means unmapped
    subjects get nothing. --use-builtin-policy defaults true, supplying the
    role:readonly / role:admin definitions the live ConfigMap references.
    """
    proc = subprocess.run(
        [ARGOCD, "admin", "settings", "rbac", "can", subject, action, resource,
         scope, "--policy-file", policy_file, "--default-role", ""],
        capture_output=True, text=True,
    )
    verdict = (proc.stdout.strip().splitlines() or [""])[-1].strip().lower()
    if verdict == "yes":
        return True
    if verdict == "no":
        return False
    return None


# --- the policy mapping itself -------------------------------------------------

def test_percona_is_mapped_to_readonly(policy_text: str):
    assert "g, percona, role:readonly" in policy_text, (
        "percona must be bound to the built-in read-only role")


def test_percona_is_not_admin(policy_text: str):
    assert "g, percona, role:admin" not in policy_text
    assert "g, grafana_cd_admins, role:admin" in policy_text


# --- what percona (all staff) CAN do: see, not touch ---------------------------

@pytest.mark.parametrize("action,resource", [
    ("get", "applications"),   # -> resource tree incl. Secret objects (values redacted)
    ("get", "repositories"),   # repo list (credentials redacted server-side)
    ("get", "clusters"),
    ("get", "logs"),
])
def test_percona_can_read(action, resource, policy_file):
    assert rbac_can("percona", action, resource, policy_file) is True, (
        f"percona/readonly expected ALLOW for {action} {resource}")


# --- what percona CANNOT do: any path to plaintext or mutation -----------------

@pytest.mark.parametrize("action,resource", [
    ("create", "exec"),         # no pod shell -> cannot print decoded secret values
    ("sync", "applications"),
    ("create", "applications"),
    ("update", "applications"),
    ("delete", "applications"),
])
def test_percona_is_denied(action, resource, policy_file):
    assert rbac_can("percona", action, resource, policy_file) is False, (
        f"percona/readonly expected DENY for {action} {resource}")


# --- contrast: the admin group is the one that can exec/mutate -----------------

@pytest.mark.parametrize("action,resource", [
    ("get", "applications"),
    ("create", "exec"),
    ("delete", "applications"),
])
def test_admins_can_do_more(action, resource, policy_file):
    assert rbac_can("grafana_cd_admins", action, resource, policy_file) is True, (
        f"grafana_cd_admins/admin expected ALLOW for {action} {resource}")


# --- default deny: a user in neither group gets nothing ------------------------

def test_unmapped_subject_denied(policy_file):
    assert rbac_can("nobody-not-in-any-group", "get", "applications",
                    policy_file) is False


if __name__ == "__main__":
    sys.exit(pytest.main([__file__, "-v"]))
