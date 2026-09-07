"""Regression tests for the snapshot-cleanup reaper. Each test locks a behavior
that must not silently regress: only managed-by=ebs-csi-driver snapshots are
even considered, the newest KEEP_COUNT per volume survive regardless of age,
the RETENTION_DAYS floor spares fresh snapshots, the opt-outs hold (PerconaKeep,
do-not-remove Name, foreign lifecycle tags), DRY_RUN gates the delete, and one
undeletable snapshot never aborts the sweep.
"""

from __future__ import annotations

import boto3
import pytest
from freezegun import freeze_time
from moto import mock_aws

pytestmark = [pytest.mark.aws]

NOW = "2026-07-01 12:00:00"      # when the reaper runs
OLD = "2026-06-01 00:00:00"      # 30 days old: well past the 14d floor
OLDER = "2026-05-01 00:00:00"    # 61 days old
YOUNG = "2026-06-30 12:00:00"    # 1 day old: below the 14d floor
REGION = "us-east-1"
CSI_TAGS = {"managed-by": "ebs-csi-driver"}


def _mk_snap(region, created_at, *, tags=None, size=8):
    with freeze_time(created_at):
        ec2 = boto3.client("ec2", region_name=region)
        vol = ec2.create_volume(AvailabilityZone=f"{region}a", Size=size)
        snap = ec2.create_snapshot(VolumeId=vol["VolumeId"])
        if tags:
            ec2.create_tags(
                Resources=[snap["SnapshotId"]],
                Tags=[{"Key": k, "Value": v} for k, v in tags.items()],
            )
        return snap["SnapshotId"]


def _mk_snaps_same_volume(region, created_ats, *, tags):
    ec2 = boto3.client("ec2", region_name=region)
    vol = ec2.create_volume(AvailabilityZone=f"{region}a", Size=8)
    ids = []
    for created_at in created_ats:
        with freeze_time(created_at):
            snap = ec2.create_snapshot(VolumeId=vol["VolumeId"])
        ec2.create_tags(
            Resources=[snap["SnapshotId"]],
            Tags=[{"Key": k, "Value": v} for k, v in tags.items()],
        )
        ids.append(snap["SnapshotId"])
    return ids


def _exists(region, snap_id):
    ec2 = boto3.client("ec2", region_name=region)
    snaps = ec2.describe_snapshots(OwnerIds=["self"])["Snapshots"]
    return snap_id in [s["SnapshotId"] for s in snaps]


def _deleted_ids(result):
    return {row[1] for row in result["deleted"]}


def _skipped(result):
    return {row[1]: row[2] for row in result["skipped"]}


def test_csi_tagged_old_snapshot_is_reaped(snapshot_cleanup, monkeypatch):
    monkeypatch.setenv("DRY_RUN", "false")
    monkeypatch.setenv("RETENTION_DAYS", "14")
    monkeypatch.setenv("KEEP_COUNT", "0")
    monkeypatch.setenv("REGIONS", REGION)
    with mock_aws():
        snap = _mk_snap(REGION, OLD, tags=CSI_TAGS, size=100)
        with freeze_time(NOW):
            result = snapshot_cleanup.lambda_handler({}, None)
        assert snap in _deleted_ids(result)
        assert not _exists(REGION, snap)


def test_untagged_snapshot_never_considered(snapshot_cleanup, monkeypatch):
    """Selection is the CSI tag filter: a snapshot without it must not appear
    in the sweep at all, deleted or skipped (DLM/Backup/AMI/CFN safety)."""
    monkeypatch.setenv("DRY_RUN", "false")
    monkeypatch.setenv("RETENTION_DAYS", "14")
    monkeypatch.setenv("KEEP_COUNT", "0")
    monkeypatch.setenv("REGIONS", REGION)
    with mock_aws():
        snap = _mk_snap(REGION, OLD, tags={"Name": "some-ami-backing-snapshot"})
        with freeze_time(NOW):
            result = snapshot_cleanup.lambda_handler({}, None)
        assert snap not in _deleted_ids(result)
        assert snap not in _skipped(result)
        assert _exists(REGION, snap)


def test_newest_keep_count_survive_regardless_of_age(snapshot_cleanup, monkeypatch):
    monkeypatch.setenv("DRY_RUN", "false")
    monkeypatch.setenv("RETENTION_DAYS", "14")
    monkeypatch.setenv("KEEP_COUNT", "2")
    monkeypatch.setenv("REGIONS", REGION)
    with mock_aws():
        oldest, middle, newest = _mk_snaps_same_volume(
            REGION, [OLDER, OLD, YOUNG], tags=CSI_TAGS
        )
        with freeze_time(NOW):
            result = snapshot_cleanup.lambda_handler({}, None)
        assert oldest in _deleted_ids(result)
        assert not _exists(REGION, oldest)
        assert _skipped(result)[newest] == "newest-2"
        assert _skipped(result)[middle] == "newest-2"
        assert _exists(REGION, newest) and _exists(REGION, middle)


def test_keep_count_is_per_volume(snapshot_cleanup, monkeypatch):
    """Two volumes with one old snapshot each and KEEP_COUNT=1: both survive.
    A global newest-N would wrongly reap one of them."""
    monkeypatch.setenv("DRY_RUN", "false")
    monkeypatch.setenv("RETENTION_DAYS", "14")
    monkeypatch.setenv("KEEP_COUNT", "1")
    monkeypatch.setenv("REGIONS", REGION)
    with mock_aws():
        a = _mk_snap(REGION, OLDER, tags=CSI_TAGS)
        b = _mk_snap(REGION, OLD, tags=CSI_TAGS)
        with freeze_time(NOW):
            result = snapshot_cleanup.lambda_handler({}, None)
        assert _deleted_ids(result) == set()
        assert _exists(REGION, a) and _exists(REGION, b)


def test_perconakeep_tag_preserved(snapshot_cleanup, monkeypatch):
    """The pre-reaper snapshots still carry PerconaKeep=True from the old
    VolumeSnapshotClass tagSpecification; they are skipped until the one-off
    untag documented in cleanup-reapers.md."""
    monkeypatch.setenv("DRY_RUN", "false")
    monkeypatch.setenv("RETENTION_DAYS", "14")
    monkeypatch.setenv("KEEP_COUNT", "0")
    monkeypatch.setenv("REGIONS", REGION)
    with mock_aws():
        snap = _mk_snap(REGION, OLD, tags={**CSI_TAGS, "PerconaKeep": "True"})
        with freeze_time(NOW):
            result = snapshot_cleanup.lambda_handler({}, None)
        assert _skipped(result)[snap] == "PerconaKeep"
        assert _exists(REGION, snap)


def test_do_not_remove_name_preserved(snapshot_cleanup, monkeypatch):
    monkeypatch.setenv("DRY_RUN", "false")
    monkeypatch.setenv("RETENTION_DAYS", "14")
    monkeypatch.setenv("KEEP_COUNT", "0")
    monkeypatch.setenv("REGIONS", REGION)
    with mock_aws():
        snap = _mk_snap(REGION, OLD, tags={**CSI_TAGS, "Name": "ps3 home, DO NOT REMOVE"})
        with freeze_time(NOW):
            result = snapshot_cleanup.lambda_handler({}, None)
        assert _skipped(result)[snap] == "name:do-not-remove"
        assert _exists(REGION, snap)


def test_young_snapshot_below_retention_preserved(snapshot_cleanup, monkeypatch):
    monkeypatch.setenv("DRY_RUN", "false")
    monkeypatch.setenv("RETENTION_DAYS", "14")
    monkeypatch.setenv("KEEP_COUNT", "0")
    monkeypatch.setenv("REGIONS", REGION)
    with mock_aws():
        snap = _mk_snap(REGION, YOUNG, tags=CSI_TAGS)
        with freeze_time(NOW):
            result = snapshot_cleanup.lambda_handler({}, None)
        assert _skipped(result)[snap] == "younger-than-14d"
        assert _exists(REGION, snap)


def test_foreign_lifecycle_tag_skipped():
    """Belt-and-braces: a snapshot carrying both the CSI tag and a DLM or AWS
    Backup marker is never deleted. Direct unit test of the pure policy helper:
    aws:* tag keys cannot be created through the real API or moto."""
    import datetime as dt
    import importlib.util
    import pathlib

    path = pathlib.Path(__file__).resolve().parents[1] / "snapshot-cleanup" / "index.py"
    spec = importlib.util.spec_from_file_location("snapshot_cleanup_unit", path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)

    old = dt.datetime(2026, 6, 1, tzinfo=dt.timezone.utc)
    cutoff = dt.datetime(2026, 6, 17, tzinfo=dt.timezone.utc)
    for marker in ("aws:dlm:lifecycle-policy-id", "aws:backup:source-resource"):
        tags = {"managed-by": "ebs-csi-driver", marker: "x"}
        assert mod._skip_reason(tags, old, cutoff, 14) == "foreign-lifecycle-tag"
    # Sanity: the same snapshot without the marker is deletable.
    assert mod._skip_reason({"managed-by": "ebs-csi-driver"}, old, cutoff, 14) is None


def test_dry_run_reports_but_deletes_nothing(snapshot_cleanup, monkeypatch):
    monkeypatch.setenv("DRY_RUN", "true")
    monkeypatch.setenv("RETENTION_DAYS", "14")
    monkeypatch.setenv("KEEP_COUNT", "0")
    monkeypatch.setenv("REGIONS", REGION)
    with mock_aws():
        snap = _mk_snap(REGION, OLD, tags=CSI_TAGS)
        with freeze_time(NOW):
            result = snapshot_cleanup.lambda_handler({}, None)
        assert snap in _deleted_ids(result)
        assert result["dry_run"] is True
        assert _exists(REGION, snap)


def test_dry_run_unset_defaults_safe(snapshot_cleanup, monkeypatch):
    monkeypatch.setenv("RETENTION_DAYS", "14")
    monkeypatch.setenv("KEEP_COUNT", "0")
    monkeypatch.setenv("REGIONS", REGION)
    with mock_aws():
        snap = _mk_snap(REGION, OLD, tags=CSI_TAGS)
        with freeze_time(NOW):
            result = snapshot_cleanup.lambda_handler({}, None)
        assert result["dry_run"] is True
        assert _exists(REGION, snap)


def test_snapshot_without_volume_id_never_deleted(snapshot_cleanup, monkeypatch):
    """VolumeId is optional in the EC2 API; without it the keep-newest-N
    grouping is undefined, so the snapshot must be skipped outright, never
    pooled into a shared keep-set. Fake client: moto always sets VolumeId."""
    import datetime as dt
    from types import SimpleNamespace

    old_time = dt.datetime(2026, 6, 1, tzinfo=dt.timezone.utc)
    pages = [{"Snapshots": [{
        "SnapshotId": "snap-no-vol",
        "VolumeSize": 8,
        "StartTime": old_time,
        "Tags": [{"Key": "managed-by", "Value": "ebs-csi-driver"}],
    }]}]
    deleted_calls = []
    fake_client = SimpleNamespace(
        get_paginator=lambda name: SimpleNamespace(paginate=lambda **kw: pages),
        delete_snapshot=lambda SnapshotId: deleted_calls.append(SnapshotId),
    )
    monkeypatch.setattr(snapshot_cleanup.boto3, "client", lambda *a, **kw: fake_client)
    monkeypatch.setenv("DRY_RUN", "false")
    monkeypatch.setenv("RETENTION_DAYS", "14")
    monkeypatch.setenv("KEEP_COUNT", "0")
    monkeypatch.setenv("REGIONS", REGION)

    with freeze_time(NOW):
        result = snapshot_cleanup.lambda_handler({}, None)
    assert deleted_calls == []
    assert _skipped(result)["snap-no-vol"] == "no-volume-id"


def test_failed_delete_does_not_abort_sweep(snapshot_cleanup, monkeypatch):
    """One undeletable snapshot (e.g. AMI-bound InvalidSnapshot.InUse) must be
    recorded and skipped, not abort the remaining snapshots. Uses a fake client:
    moto cannot bind an AMI to a CSI snapshot mid-sweep."""
    import datetime as dt
    from types import SimpleNamespace

    from botocore.exceptions import ClientError

    old_time = dt.datetime(2026, 6, 1, tzinfo=dt.timezone.utc)

    def _snap(sid, vol):
        return {
            "SnapshotId": sid,
            "VolumeId": vol,
            "VolumeSize": 8,
            "StartTime": old_time,
            "Tags": [{"Key": "managed-by", "Value": "ebs-csi-driver"}],
        }

    pages = [{"Snapshots": [_snap("snap-fails", "vol-a"), _snap("snap-ok", "vol-b")]}]

    def delete_snapshot(SnapshotId):
        if SnapshotId == "snap-fails":
            raise ClientError(
                {"Error": {"Code": "InvalidSnapshot.InUse", "Message": "ami-bound"}},
                "DeleteSnapshot",
            )

    fake_client = SimpleNamespace(
        get_paginator=lambda name: SimpleNamespace(paginate=lambda **kw: pages),
        delete_snapshot=delete_snapshot,
    )
    monkeypatch.setattr(snapshot_cleanup.boto3, "client", lambda *a, **kw: fake_client)
    monkeypatch.setenv("DRY_RUN", "false")
    monkeypatch.setenv("RETENTION_DAYS", "14")
    monkeypatch.setenv("KEEP_COUNT", "0")
    monkeypatch.setenv("REGIONS", REGION)

    with freeze_time(NOW):
        result = snapshot_cleanup.lambda_handler({}, None)
    assert "snap-ok" in _deleted_ids(result)
    assert _skipped(result)["snap-fails"] == "error:InvalidSnapshot.InUse"
