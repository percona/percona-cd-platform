"""Static assertions over the master role policy in main.tf and the masters
that turn on the EC2 plugin metadata enforcement switch.

The master role is shared by every master that uses this module, so a grant
the EC2 plugin needs at launch time must sit in the master policy and stay
out of the worker policy.
"""

from __future__ import annotations

import re
from pathlib import Path

MAIN_TF = Path(__file__).resolve().parents[1] / "main.tf"
REPO = Path(__file__).resolve().parents[4]


def _policy_document(name: str) -> str:
    text = MAIN_TF.read_text()
    start = text.index(f'data "aws_iam_policy_document" "{name}" {{')
    end = text.find('\ndata "', start + 1)
    return text[start:end]


def _actions(block: str) -> list[str]:
    return re.findall(r'"(ec2:[A-Za-z]+)"', block)


def test_master_can_enforce_worker_metadata_options() -> None:
    """With hudson.plugins.ec2.enforceMetadataOptions on, the plugin calls
    ModifyInstanceMetadataOptions on each new spot worker and terminates the
    worker when the call fails."""
    actions = _actions(_policy_document("master_start_instances"))
    assert "ec2:ModifyInstanceMetadataOptions" in actions


def test_worker_role_cannot_modify_metadata_options() -> None:
    actions = _actions(_policy_document("worker"))
    assert "ec2:ModifyInstanceMetadataOptions" not in actions


def test_only_ps80_enforces_metadata_options_before_clouds_exist() -> None:
    """The property must be set before the classic clouds are created, and
    no other master turns it on."""
    masters = REPO / "resources" / "jenkins-masters"
    setter = "System.setProperty('hudson.plugins.ec2.enforceMetadataOptions', 'true')"
    on = sorted(
        str(p.relative_to(masters))
        for p in masters.rglob("*.groovy*")
        if p.is_file() and "enforceMetadataOptions" in p.read_text()
    )
    assert on == ["ps80/init.groovy.d/cloud.groovy.tftpl"]
    text = (masters / on[0]).read_text()
    assert text.index(setter) < text.index("new EC2Cloud(")
