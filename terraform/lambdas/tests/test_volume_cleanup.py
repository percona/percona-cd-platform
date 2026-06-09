"""Regression tests for the volume-cleanup reaper. Each test locks a behavior
that must not silently regress: untagged volumes are reaped, None tags don't
crash, the MIN_AGE_HOURS floor spares fresh volumes, the opt-outs hold, DRY_RUN
gates the delete, and attached volumes are left alone.
"""

from __future__ import annotations

import boto3
import pytest
from freezegun import freeze_time
from moto import mock_aws

pytestmark = [pytest.mark.aws]

NOW = "2026-06-09 18:00:00"      # when the reaper runs
OLD = "2026-06-01 00:00:00"      # 8 days old: well past the 24h floor
YOUNG = "2026-06-09 17:30:00"    # 30 min old: below the 24h floor
PRIMARY = "us-east-1"
SECOND = "eu-central-1"
AMI = "ami-12c6146b"


def _mk(region, created_at, *, tags=None, size=8):
    with freeze_time(created_at):
        ec2 = boto3.resource("ec2", region_name=region)
        vol = ec2.create_volume(AvailabilityZone=f"{region}a", Size=size)
        if tags:
            vol.create_tags(Tags=[{"Key": k, "Value": v} for k, v in tags.items()])
        return vol.id


def _exists(region, vol_id):
    ec2 = boto3.resource("ec2", region_name=region)
    return vol_id in [v.id for v in ec2.volumes.all()]


def _deleted_ids(result):
    return {row[1] for row in result["deleted"]}


def _skipped(result):
    return {row[1]: row[2] for row in result["skipped"]}


def test_untagged_old_volume_is_reaped(volume_cleanup, monkeypatch):
    monkeypatch.setenv("DRY_RUN", "false")  # handler now defaults to dry-run
    monkeypatch.setenv("MIN_AGE_HOURS", "24")
    monkeypatch.setenv("REGIONS", f"{PRIMARY},{SECOND}")
    with mock_aws():
        v1 = _mk(PRIMARY, OLD, size=30)   # untagged -> volume.tags is None
        v2 = _mk(SECOND, OLD)
        with freeze_time(NOW):
            result = volume_cleanup.lambda_handler({}, None)
        assert {v1, v2} <= _deleted_ids(result)
        assert not _exists(PRIMARY, v1)
        assert not _exists(SECOND, v2)


def test_perconakeep_tag_preserved(volume_cleanup, monkeypatch):
    monkeypatch.setenv("MIN_AGE_HOURS", "24")
    monkeypatch.setenv("REGIONS", PRIMARY)
    with mock_aws():
        vid = _mk(PRIMARY, OLD, tags={"PerconaKeep": "true"})
        with freeze_time(NOW):
            result = volume_cleanup.lambda_handler({}, None)
        assert vid not in _deleted_ids(result)
        assert _skipped(result)[vid] == "PerconaKeep"
        assert _exists(PRIMARY, vid)


def test_do_not_remove_name_preserved(volume_cleanup, monkeypatch):
    monkeypatch.setenv("MIN_AGE_HOURS", "24")
    monkeypatch.setenv("REGIONS", PRIMARY)
    with mock_aws():
        vid = _mk(PRIMARY, OLD, tags={"Name": "Prod DB - Do Not Remove"})  # mixed case
        with freeze_time(NOW):
            result = volume_cleanup.lambda_handler({}, None)
        assert vid not in _deleted_ids(result)
        assert _skipped(result)[vid] == "name:do-not-remove"
        assert _exists(PRIMARY, vid)


def test_name_matched_by_key_not_position(volume_cleanup, monkeypatch):
    monkeypatch.setenv("MIN_AGE_HOURS", "24")
    monkeypatch.setenv("REGIONS", PRIMARY)
    with mock_aws():
        vid = _mk(PRIMARY, OLD, tags={"environment": "ci", "Name": "scratch - do not remove"})
        with freeze_time(NOW):
            result = volume_cleanup.lambda_handler({}, None)
        assert vid not in _deleted_ids(result)
        assert _exists(PRIMARY, vid)


def test_young_volume_below_min_age_preserved(volume_cleanup, monkeypatch):
    monkeypatch.setenv("DRY_RUN", "false")  # so the old volume is actually deleted
    monkeypatch.setenv("MIN_AGE_HOURS", "24")
    monkeypatch.setenv("REGIONS", PRIMARY)
    with mock_aws():
        young = _mk(PRIMARY, YOUNG)
        old = _mk(PRIMARY, OLD)
        with freeze_time(NOW):
            result = volume_cleanup.lambda_handler({}, None)
        assert young not in _deleted_ids(result)
        assert _skipped(result)[young] == "younger-than-24h"
        assert _exists(PRIMARY, young)
        assert old in _deleted_ids(result)
        assert not _exists(PRIMARY, old)


def test_dry_run_reports_but_deletes_nothing(volume_cleanup, monkeypatch):
    monkeypatch.setenv("MIN_AGE_HOURS", "24")
    monkeypatch.setenv("DRY_RUN", "true")
    monkeypatch.setenv("REGIONS", PRIMARY)
    with mock_aws():
        vid = _mk(PRIMARY, OLD)
        with freeze_time(NOW):
            result = volume_cleanup.lambda_handler({}, None)
        assert result["dry_run"] is True
        assert vid in _deleted_ids(result)   # reported as would-delete
        assert _exists(PRIMARY, vid)          # but still present


def test_dry_run_unset_defaults_safe(volume_cleanup, monkeypatch):
    """With DRY_RUN absent from the environment, the reaper must not delete."""
    monkeypatch.setenv("MIN_AGE_HOURS", "24")
    monkeypatch.setenv("REGIONS", PRIMARY)  # DRY_RUN deliberately NOT set
    with mock_aws():
        vid = _mk(PRIMARY, OLD)
        with freeze_time(NOW):
            result = volume_cleanup.lambda_handler({}, None)
        assert result["dry_run"] is True
        assert _exists(PRIMARY, vid)


def test_failed_delete_does_not_abort_sweep(volume_cleanup, monkeypatch):
    """One undeletable volume (the available->in-use race raising VolumeInUse)
    must be recorded and skipped, not abort the remaining volumes/regions.
    Uses fake resource objects: moto cannot interleave an attach mid-sweep."""
    import datetime as dt
    from types import SimpleNamespace

    from botocore.exceptions import ClientError

    old_time = dt.datetime(2026, 6, 1, tzinfo=dt.timezone.utc)

    def _vol(vid, fail):
        def delete():
            if fail:
                raise ClientError(
                    {"Error": {"Code": "VolumeInUse", "Message": "busy"}}, "DeleteVolume"
                )
        return SimpleNamespace(id=vid, size=8, tags=None, create_time=old_time, delete=delete)

    volumes = [_vol("vol-fails", True), _vol("vol-ok", False)]
    fake_ec2 = SimpleNamespace(
        volumes=SimpleNamespace(filter=lambda Filters: volumes)
    )
    monkeypatch.setattr(volume_cleanup.boto3, "resource", lambda *a, **kw: fake_ec2)
    monkeypatch.setenv("DRY_RUN", "false")
    monkeypatch.setenv("MIN_AGE_HOURS", "24")
    monkeypatch.setenv("REGIONS", PRIMARY)

    result = volume_cleanup.lambda_handler({}, None)
    assert ("vol-ok") in {row[1] for row in result["deleted"]}
    assert _skipped(result)["vol-fails"] == "error:VolumeInUse"


def test_in_use_volume_ignored(volume_cleanup, monkeypatch):
    monkeypatch.setenv("MIN_AGE_HOURS", "24")
    monkeypatch.setenv("REGIONS", PRIMARY)
    with mock_aws():
        with freeze_time(OLD):
            ec2 = boto3.resource("ec2", region_name=PRIMARY)
            inst = ec2.create_instances(
                ImageId=AMI, MinCount=1, MaxCount=1,
                Placement={"AvailabilityZone": f"{PRIMARY}a"},
            )[0]
            vol = ec2.create_volume(AvailabilityZone=f"{PRIMARY}a", Size=8)
            vol.attach_to_instance(InstanceId=inst.id, Device="/dev/sdf")
            vid = vol.id
        with freeze_time(NOW):
            result = volume_cleanup.lambda_handler({}, None)
        assert vid not in _deleted_ids(result)
        assert _exists(PRIMARY, vid)
