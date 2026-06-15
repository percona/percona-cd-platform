# /// script
# requires-python = ">=3.11"
# dependencies = ["pytest>=8", "boto3>=1.34"]
# ///
"""
Live audit: who can read the cluster's AWS Secrets Manager secrets DIRECTLY
from AWS (bypassing the cluster).

This is a LIVE integration test, not a PR-gate unit test: it calls boto3
against the real account and (for the drift check) kubectl against the live
cluster. CI has no AWS credentials, so it is excluded from the pytest job and
run on demand via `just audit-secret-access` (or scheduled out-of-band). The
static, no-AWS gate that keeps the fence in the Terraform lives at
terraform/lambdas/tests/test_secret_resource_policies.py.

The cluster consumes 7 secrets via External Secrets Operator (ESO). Direct AWS
read access to each is decided by (a) the secret's resource policy and (b)
identity-based IAM, since all 7 use the AWS-managed KMS key (no CMK gate).

Posture model:
  * HARD invariants (fail = real regression):
      - every classified secret exists
      - no secret Allows GetSecretValue to a foreign account or wildcard principal
      - each fenced secret denies the running auditor (a broad-SM-read admin),
        its allowlist is exactly the ESO role, break-glass is empty
      - live ESO ExternalSecrets reference only classified secrets (drift guard)
  * GAP trackers (xfail strict; auto-flip when remediated): grafana/admin SHOULD
    be ESO-role-only but is still admin-readable until secrets-policies.tf is
    applied. When the apply lands, admin read becomes DENIED, the test XPASSes,
    strict-xfail fails, and the secret must be moved from GAP to FENCED here.

No secret values are printed. get_secret_value is invoked only to probe
allow/deny; the returned value is never bound to a name or logged.
"""
from __future__ import annotations

import json
import os
import subprocess
import sys

import boto3
import pytest
from botocore.exceptions import BotoCoreError, ClientError

REGION = os.environ.get("AWS_REGION", "us-east-1")
PROFILE = os.environ.get("AWS_PROFILE")
ESO_ROLE_SUBSTR = "role/percona-ci-platform-external-secrets-"

# Classification of the cluster's secrets.
#   "fenced"   -> must be ESO-role-only (admin DENIED). Asserted hard.
#   "gap"      -> SHOULD be ESO-role-only; admin-readable until apply (xfail strict).
#   "app-cred" -> single-app credential; prefix-scoped IAM read accepted today.
SECRETS = {
    "percona-ci-platform/authentik/config":             "fenced",
    "percona-ci-platform/authentik/saml/private_key":   "fenced",
    "percona-ci-platform/grafana/admin":                "gap",
    "percona-ci-platform/authentik/saml/certificate":   "app-cred",
    "percona-ci-platform/alloy-gateway/bearer":         "app-cred",
    "percona-ci-platform/jenkins/ps3/github-oauth":     "app-cred",
    "percona-ci-platform/mtr/config":                   "app-cred",
}
FENCED = [s for s, c in SECRETS.items() if c == "fenced"]
GAP = [s for s, c in SECRETS.items() if c == "gap"]
OPEN = [s for s, c in SECRETS.items() if c == "app-cred"]


def _session() -> boto3.Session:
    s = boto3.Session(region_name=REGION)
    if PROFILE and PROFILE in s.available_profiles:
        s = boto3.Session(profile_name=PROFILE, region_name=REGION)
    return s


@pytest.fixture(scope="session")
def sm():
    sess = _session()
    try:
        sess.client("sts").get_caller_identity()
    except (BotoCoreError, ClientError) as e:
        pytest.skip(f"no usable AWS credentials: {e}")
    return sess.client("secretsmanager")


@pytest.fixture(scope="session")
def account_id() -> str:
    return _session().client("sts").get_caller_identity()["Account"]


def _resource_policy(sm, sid: str) -> dict | None:
    raw = sm.get_resource_policy(SecretId=sid).get("ResourcePolicy")
    return json.loads(raw) if raw else None


def _admin_can_read(sm, sid: str) -> bool:
    """True if the running principal can GetSecretValue. Value is discarded."""
    try:
        sm.get_secret_value(SecretId=sid)  # value intentionally not bound/printed
        return True
    except ClientError as e:
        if e.response["Error"]["Code"] in ("AccessDeniedException", "AccessDenied"):
            return False
        raise


@pytest.fixture(scope="session")
def privileged(sm) -> bool:
    """Confirm we audit as a broad-SM-read principal, else access claims are moot."""
    return _admin_can_read(sm, "percona-ci-platform/mtr/config")


# --- HARD invariants ----------------------------------------------------------

@pytest.mark.parametrize("sid", list(SECRETS))
def test_classified_secret_exists(sm, sid):
    sm.describe_secret(SecretId=sid)  # raises ResourceNotFound if gone


@pytest.mark.parametrize("sid", list(SECRETS))
def test_no_public_or_cross_account_allow(sm, account_id, sid):
    """A secret must never Allow GetSecretValue to '*' or a foreign account."""
    pol = _resource_policy(sm, sid)
    if pol is None:
        return  # no resource policy -> no foreign allow possible here
    for st in pol.get("Statement", []):
        if st.get("Effect") != "Allow":
            continue
        actions = st.get("Action", [])
        actions = [actions] if isinstance(actions, str) else actions
        if not any("GetSecretValue" in a or a == "secretsmanager:*" for a in actions):
            continue
        principals = st.get("Principal", {})
        vals = []
        if principals == "*":
            vals = ["*"]
        elif isinstance(principals, dict):
            for v in principals.values():
                vals += [v] if isinstance(v, str) else list(v)
        for v in vals:
            assert v != "*", f"{sid}: Allows GetSecretValue to wildcard principal"
            assert account_id in v or v.startswith("arn:aws:iam::aws:"), (
                f"{sid}: Allows GetSecretValue to foreign principal {v}")


@pytest.mark.parametrize("sid", FENCED)
def test_fenced_secret_blocks_admin(sm, privileged, sid):
    if not privileged:
        pytest.skip("auditor lacks broad SM read; cannot assert fence")
    assert _admin_can_read(sm, sid) is False, (
        f"{sid} must DENY GetSecretValue to admins (deny-by-default fence)")


@pytest.mark.parametrize("sid", FENCED)
def test_fenced_secret_allowlist_is_eso_only(sm, sid):
    pol = _resource_policy(sm, sid)
    assert pol is not None, f"{sid} must carry a resource policy"
    deny = [s for s in pol["Statement"] if s.get("Effect") == "Deny"]
    assert deny, f"{sid}: expected a deny-by-default statement"
    allowlist = deny[0]["Condition"]["StringNotLike"]["aws:PrincipalArn"]
    allowlist = [allowlist] if isinstance(allowlist, str) else allowlist
    # exactly the ESO role, nothing else -> break-glass allowlist is empty
    assert len(allowlist) == 1, f"{sid}: break-glass must be empty; allowlist={allowlist}"
    assert ESO_ROLE_SUBSTR in allowlist[0], (
        f"{sid}: sole allowed principal must be the ESO role; got {allowlist[0]}")


def test_open_app_creds_have_no_resource_policy(sm):
    """Characterize the accepted state: app-cred secrets rely on prefix IAM only.
    If one later gains a resource policy (i.e. gets hardened), this flips and
    prompts moving it from OPEN to FENCED in this file."""
    for sid in OPEN:
        assert _resource_policy(sm, sid) is None, (
            f"{sid} unexpectedly has a resource policy; reclassify it in SECRETS")


# --- GAP trackers (auto-flip when remediated) ---------------------------------

@pytest.mark.parametrize("sid", [
    pytest.param(s, marks=pytest.mark.xfail(
        strict=True,
        reason="finding B2: should be ESO-role-only; fenced once secrets-policies.tf applies"))
    for s in GAP
])
def test_high_value_secret_is_fenced(sm, privileged, sid):
    if not privileged:
        pytest.skip("auditor lacks broad SM read; cannot assert fence")
    assert _admin_can_read(sm, sid) is False, (
        f"{sid} should DENY admins once fenced")


# --- drift guard --------------------------------------------------------------

def test_live_externalsecrets_are_all_classified():
    """Every SM key referenced by a live ESO ExternalSecret must be classified."""
    try:
        out = subprocess.run(
            ["kubectl", "get", "externalsecrets.external-secrets.io", "-A", "-o", "json"],
            capture_output=True, text=True, timeout=30)
    except (FileNotFoundError, subprocess.TimeoutExpired):
        pytest.skip("kubectl unavailable; skipping live drift check")
    if out.returncode != 0:
        pytest.skip(f"kubectl error: {out.stderr.strip()}")
    live = set()
    for it in json.loads(out.stdout)["items"]:
        for e in it["spec"].get("data", []):
            k = (e.get("remoteRef") or {}).get("key")
            if k:
                live.add(k)
        for e in it["spec"].get("dataFrom", []):
            k = (e.get("extract") or {}).get("key")
            if k:
                live.add(k)
    unclassified = live - set(SECRETS)
    assert not unclassified, (
        f"new cluster secret(s) not classified in this audit: {sorted(unclassified)}")


if __name__ == "__main__":
    sys.exit(pytest.main([__file__, "-v"]))
