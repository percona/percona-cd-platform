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

import pytest

pytestmark = [pytest.mark.iam]

TF_DIR = pathlib.Path(__file__).resolve().parents[2]  # terraform/
POLICY = "secrets-policies.tf"


def _tf(name: str) -> str:
    return (TF_DIR / name).read_text()


def test_both_high_value_secrets_are_fenced() -> None:
    tf = _tf(POLICY)
    assert "percona-ci-platform/grafana/admin" in tf
    assert "percona-ci-platform/authentik/saml/private_key" in tf


def test_public_saml_cert_is_not_fenced() -> None:
    """The certificate half is the public SP cert; fencing it adds no security
    and would only risk breaking ESO sync. (The comment may name it; what must
    be absent is the quoted secret name as a fenced_secrets map entry.)"""
    assert '"percona-ci-platform/authentik/saml/certificate"' not in _tf(POLICY)


def test_fence_is_deny_get_secret_value() -> None:
    tf = _tf(POLICY)
    assert "aws_secretsmanager_secret_policy" in tf
    assert '"DenyGetSecretValueExceptAllowlist"' in tf
    assert '"Deny"' in tf
    assert '"secretsmanager:GetSecretValue"' in tf


def test_fence_allowlists_only_eso_role_plus_breakglass() -> None:
    """Deny everyone except the ESO pod-identity role and the (empty by default)
    break-glass var. A wider allowlist would defeat the fence."""
    tf = _tf(POLICY)
    assert "StringNotLike" in tf  # HCL identifier key, unquoted
    assert '"aws:PrincipalArn"' in tf
    assert "module.pod_identity_external_secrets.iam_role_arn" in tf
    assert "var.fenced_secret_breakglass_arns" in tf


def test_fence_attaches_via_data_source_lookup() -> None:
    """Both secrets are created out-of-band, so the policy must resolve the ARN
    through a data source rather than a managed aws_secretsmanager_secret."""
    tf = _tf(POLICY)
    assert 'data "aws_secretsmanager_secret" "fenced"' in tf
    assert "secret_arn = data.aws_secretsmanager_secret.fenced" in tf


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
