"""Static assertions that the out-of-band, ESO-synced Secrets Manager secrets
carry a deny-by-default resource policy.

These secrets hold high-blast-radius credentials (Grafana server-admin, the
SAML SP signing key). Without the resource-policy fence, any principal with
broad secretsmanager:GetSecretValue (every AdministratorAccess holder) can read
them directly from AWS. moto cannot prove a resource policy, so this locks the
policy shape as a plain-text regression on the *.tf source (finding B2,
docs/security-review-2026-05-07.md). Assertions match tokens that survive
`tofu fmt`, not exact whitespace.
"""

from __future__ import annotations

import pathlib
import re

import pytest

pytestmark = [pytest.mark.iam]

TF_DIR = pathlib.Path(__file__).resolve().parents[2]  # terraform/
POLICY = "secrets-policies.tf"


def _tf(name: str) -> str:
    return (TF_DIR / name).read_text()


def _fenced_map(tf: str) -> str:
    """The body of the `fenced_secrets = { ... }` local, comments excluded."""
    m = re.search(r"fenced_secrets\s*=\s*\{(.*?)\}", tf, re.DOTALL)
    assert m, "fenced_secrets map not found in secrets-policies.tf"
    return "\n".join(
        ln for ln in m.group(1).splitlines() if not ln.lstrip().startswith("#"))


def _policy_block(tf: str) -> str:
    """From the policy resource declaration to EOF (it is the last resource)."""
    m = re.search(
        r'resource\s+"aws_secretsmanager_secret_policy"\s+"fenced".*', tf, re.DOTALL)
    assert m, "aws_secretsmanager_secret_policy.fenced resource not found"
    return m.group(0)


def test_both_high_value_secrets_are_fenced() -> None:
    """The two secret names must be VALUES in the fenced_secrets map, not just
    mentioned in a comment somewhere."""
    m = _fenced_map(_tf(POLICY))
    assert re.search(r'=\s*"percona-ci-platform/grafana/admin"', m)
    assert re.search(r'=\s*"percona-ci-platform/authentik/saml/private_key"', m)


def test_public_saml_cert_is_not_fenced() -> None:
    """The certificate half is the public SP cert; fencing it adds no security
    and would only risk breaking ESO sync. It must not be a map entry (a comment
    mention is fine)."""
    m = _fenced_map(_tf(POLICY))
    assert not re.search(r'=\s*"percona-ci-platform/authentik/saml/certificate"', m)


def test_fence_is_deny_get_secret_value() -> None:
    block = _policy_block(_tf(POLICY))
    assert '"DenyGetSecretValueExceptAllowlist"' in block
    assert re.search(r'Effect\s*=\s*"Deny"', block)
    assert re.search(r'Action\s*=\s*"secretsmanager:GetSecretValue"', block)
    assert re.search(r'Principal\s*=\s*"\*"', block)


def test_fence_allowlists_only_eso_role_plus_breakglass() -> None:
    """Deny everyone except the ESO pod-identity role and the (empty by default)
    break-glass var. A wider allowlist would defeat the fence."""
    block = _policy_block(_tf(POLICY))
    assert re.search(r'StringNotLike\s*=\s*\{', block)
    assert '"aws:PrincipalArn"' in block
    assert "module.pod_identity_external_secrets.iam_role_arn" in block
    assert "var.fenced_secret_breakglass_arns" in block


def test_fence_attaches_via_data_source_lookup() -> None:
    """Both secrets are created out-of-band, so the policy must resolve the ARN
    through a data source rather than a managed aws_secretsmanager_secret."""
    tf = _tf(POLICY)
    assert 'data "aws_secretsmanager_secret" "fenced"' in tf
    assert "secret_arn = data.aws_secretsmanager_secret.fenced" in _policy_block(tf)


def test_authentik_config_fence_still_present() -> None:
    """Regression guard: the original authentik/config fence must not be lost."""
    tf = _tf("authentik.tf")
    assert '"aws_secretsmanager_secret_policy" "authentik_config"' in tf
    assert '"DenyGetSecretValueExceptAllowlist"' in tf


def test_breakglass_var_defined_and_empty_by_default() -> None:
    tf = _tf("variables.tf")
    assert 'variable "fenced_secret_breakglass_arns"' in tf
    # default empty -> only the ESO role can read until an operator opts in
    assert "default     = []" in tf
