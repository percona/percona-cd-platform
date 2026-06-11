"""The runbook plan gates as pure functions (the safety-critical paths)."""

from __future__ import annotations

from cdpctl.runbook import plan_changes, scope_violations


def _plan(*resource_changes):
    return {"resource_changes": list(resource_changes)}


def _rc(address, actions):
    return {"address": address, "change": {"actions": actions}}


def test_plan_changes_drops_noops_and_reads():
    plan = _plan(
        _rc('module.ps57.aws_s3_object.init_config["cloud.groovy"]', ["update"]),
        _rc("module.ps57.aws_instance.master", ["no-op"]),
        _rc("data.aws_ami.al2023", ["read"]),
    )
    assert plan_changes(plan) == [
        ('module.ps57.aws_s3_object.init_config["cloud.groovy"]', ["update"]),
    ]


def test_scope_violations_passes_only_that_masters_s3_updates():
    changes = [
        ('module.ps57.aws_s3_object.init_config["cloud.groovy"]', ["update"]),
        ('module.ps57.aws_s3_object.init_config["matrix.groovy"]', ["update"]),
    ]
    assert scope_violations(changes, "ps57") == []


def test_scope_violations_flags_other_modules_and_non_updates():
    changes = [
        ('module.ps57.aws_s3_object.init_config["cloud.groovy"]', ["update"]),
        ('module.ps80.aws_s3_object.init_config["cloud.groovy"]', ["update"]),
        ("module.ps57.aws_instance.master", ["delete", "create"]),
        ('module.ps57.aws_s3_object.init_config["new.groovy"]', ["create"]),
    ]
    bad = scope_violations(changes, "ps57")
    assert ('module.ps57.aws_s3_object.init_config["cloud.groovy"]', ["update"]) not in bad
    assert len(bad) == 3
