"""Static assertions over the least-privilege IAM rendered in the root
instantiation files. moto cannot prove IAM, so these lock the load-bearing
policy decisions as plain-text regressions on the *.tf source.
"""

from __future__ import annotations

import pathlib

import pytest

pytestmark = [pytest.mark.iam]

TF_DIR = pathlib.Path(__file__).resolve().parents[2]  # terraform/


def _tf(name: str) -> str:
    return (TF_DIR / name).read_text()


def test_ec2_policy_grants_delete_failed_remediation_actions() -> None:
    """The handler's DELETE_FAILED remediation calls these; the policy must
    grant all three or remediation AccessDenies at runtime."""
    tf = _tf("ec2-cleanup.tf")
    for action in (
        "ec2:RevokeSecurityGroupIngress",
        "ec2:DisassociateRouteTable",
        "ec2:DeleteRoute",
    ):
        assert action in tf, f"ec2-cleanup policy is missing {action}"


def test_ec2_terminate_scoped_to_instance_arn() -> None:
    tf = _tf("ec2-cleanup.tf")
    assert "ec2:TerminateInstances" in tf
    assert ":instance/*" in tf


def test_ec2_no_create_tags_grant() -> None:
    """The Cirrus auto-tag path was removed with Cirrus CI's 2026-06-01
    shutdown; the policy must not regain a tagging grant."""
    tf = _tf("ec2-cleanup.tf")
    assert "ec2:CreateTags" not in tf


def test_eksctl_stack_actions_scoped() -> None:
    tf = _tf("ec2-cleanup.tf")
    assert "cloudformation:DeleteStack" in tf
    assert "stack/eksctl-*-cluster/*" in tf


def test_volume_delete_scoped_to_volume_arn() -> None:
    tf = _tf("volume-cleanup.tf")
    assert "ec2:DeleteVolume" in tf
    assert ":volume/*" in tf


def test_no_requested_region_condition_anywhere() -> None:
    """The reapers iterate regions in code, so an aws:RequestedRegion condition
    would silently break cross-region cleanup."""
    # Match the HCL condition form, not the comment that explains its absence.
    for name in ("ec2-cleanup.tf", "volume-cleanup.tf"):
        assert 'variable = "aws:RequestedRegion"' not in _tf(name), f"{name} must not pin RequestedRegion"
