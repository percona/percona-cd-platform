"""Daily reaper of aged-out ebs-csi (snapscheduler) EBS snapshots.

The in-cluster snapscheduler takes a daily VolumeSnapshot of the Jenkins
controller home and prunes the Kubernetes objects past 14 retained. The
ebs-csi-retain VolumeSnapshotClass keeps ``deletionPolicy: Retain`` on purpose
(a GitOps prune or namespace teardown must not erase recovery points), so each
pruned object orphans its physical EBS snapshot and nothing ever deleted those:
unbounded +1/day growth. This reaper owns the physical aging instead:

* selects only snapshots tagged ``managed-by=ebs-csi-driver`` (stamped by the
  VolumeSnapshotClass at create time), so DLM-managed, AWS Backup, AMI and
  CloudFormation snapshots are structurally out of scope,
* keeps the newest ``KEEP_COUNT`` per source volume regardless of age,
* deletes the rest only past the ``RETENTION_DAYS`` floor,
* skips any snapshot without a ``VolumeId`` (the keep-newest grouping would
  be undefined, so it fails closed),
* keeps the account opt-outs: a ``PerconaKeep`` tag, or ``do not remove`` in
  the Name (case-insensitive),
* skips anything carrying a foreign lifecycle marker (``aws:dlm:*`` or
  ``aws:backup:*`` tags) even if it also carries the CSI tag,
* gates the actual delete behind ``DRY_RUN``,
* isolates failures: one undeletable snapshot (e.g. an AMI-bound
  ``InvalidSnapshot.InUse``) or one broken region never aborts the sweep,
* returns structured data instead of only logging.

IAM is the second, fail-closed gate: the ``ec2:DeleteSnapshot`` grant carries
an ``aws:ResourceTag/managed-by = ebs-csi-driver`` condition
(terraform/snapshot-cleanup.tf), so a handler bug cannot reach snapshots
outside the CSI class.

Environment (read at invocation, so a Terraform ``DRY_RUN`` flip is a plain
apply, and tests can pin values deterministically):

* ``DRY_RUN``        -- ``"true"``/``"false"`` (default ``"true"``: safe; the
  Terraform instantiation sets it explicitly)
* ``RETENTION_DAYS`` -- int (default ``14``; non-numeric values fall back to 14)
* ``KEEP_COUNT``     -- int (default ``14``; non-numeric values fall back to 14)
* ``REGIONS``        -- CSV of regions (default ``us-east-1`` only: the CSI
  driver creates snapshots only in the cluster region)
"""

from __future__ import annotations

import logging
import os
from datetime import datetime, timedelta, timezone
from typing import Any

import boto3
from botocore.exceptions import ClientError

logger = logging.getLogger()
logger.setLevel("INFO")

DEFAULT_REGIONS: list[str] = ["us-east-1"]
CSI_TAG_KEY: str = "managed-by"
CSI_TAG_VALUE: str = "ebs-csi-driver"
KEEP_TAG: str = "PerconaKeep"
KEEP_NAME_SUBSTR: str = "do not remove"
FOREIGN_LIFECYCLE_PREFIXES: tuple[str, ...] = ("aws:dlm:", "aws:backup:")
DEFAULT_RETENTION_DAYS: int = 14
DEFAULT_KEEP_COUNT: int = 14

# (region, snapshot_id, volume_id, size_gib)
DeletedRow = tuple[str, str, str, int]
# (region, snapshot_id, reason)
SkippedRow = tuple[str, str, str]


def _regions() -> list[str]:
    raw = os.environ.get("REGIONS", "").strip()
    return [r.strip() for r in raw.split(",") if r.strip()] or DEFAULT_REGIONS


def _retention_days() -> int:
    raw = os.environ.get("RETENTION_DAYS", str(DEFAULT_RETENTION_DAYS))
    try:
        return int(raw)
    except ValueError:
        logger.error(f"Invalid RETENTION_DAYS '{raw}'; falling back to {DEFAULT_RETENTION_DAYS}")
        return DEFAULT_RETENTION_DAYS


def _keep_count() -> int:
    raw = os.environ.get("KEEP_COUNT", str(DEFAULT_KEEP_COUNT))
    try:
        return int(raw)
    except ValueError:
        logger.error(f"Invalid KEEP_COUNT '{raw}'; falling back to {DEFAULT_KEEP_COUNT}")
        return DEFAULT_KEEP_COUNT


def _tags(snapshot: dict[str, Any]) -> dict[str, str]:
    # Tags is absent when the snapshot has none (cannot happen for CSI-created
    # ones, which match the selection filter, but keep the sweep total).
    return {t["Key"]: t.get("Value", "") for t in (snapshot.get("Tags") or [])}


def _skip_reason(
    tags: dict[str, str],
    start_time: datetime,
    cutoff: datetime,
    retention_days: int,
) -> str | None:
    """Pure per-snapshot policy for one snapshot already past the KEEP_COUNT
    set. Returns a skip reason, or None when the snapshot is deletable."""
    if KEEP_TAG in tags:
        return "PerconaKeep"
    if KEEP_NAME_SUBSTR in tags.get("Name", "").lower():
        return "name:do-not-remove"
    if any(key.startswith(FOREIGN_LIFECYCLE_PREFIXES) for key in tags):
        return "foreign-lifecycle-tag"
    if start_time > cutoff:
        return f"younger-than-{retention_days}d"
    return None


def _process_region(
    region: str,
    cutoff: datetime,
    retention_days: int,
    keep_count: int,
    dry_run: bool,
    deleted: list[DeletedRow],
    skipped: list[SkippedRow],
) -> None:
    ec2 = boto3.client("ec2", region_name=region)
    by_volume: dict[str, list[dict[str, Any]]] = {}
    paginator = ec2.get_paginator("describe_snapshots")
    for page in paginator.paginate(
        OwnerIds=["self"],
        Filters=[{"Name": f"tag:{CSI_TAG_KEY}", "Values": [CSI_TAG_VALUE]}],
    ):
        for snap in page["Snapshots"]:
            volume_id = snap.get("VolumeId")
            if not volume_id:
                # VolumeId is optional in the EC2 API. Without it the
                # keep-newest-N grouping is undefined, so never delete.
                skipped.append((region, snap["SnapshotId"], "no-volume-id"))
                continue
            by_volume.setdefault(volume_id, []).append(snap)

    for volume_id, snaps in by_volume.items():
        # Newest first. SnapshotId tie-break keeps the order deterministic when
        # several snapshots share a StartTime second.
        snaps.sort(key=lambda s: (s["StartTime"], s["SnapshotId"]), reverse=True)
        for index, snap in enumerate(snaps):
            snap_id, size = snap["SnapshotId"], snap.get("VolumeSize", 0)
            if index < keep_count:
                skipped.append((region, snap_id, f"newest-{keep_count}"))
                continue
            reason = _skip_reason(_tags(snap), snap["StartTime"], cutoff, retention_days)
            if reason:
                skipped.append((region, snap_id, reason))
                continue
            if dry_run:
                logger.info(f"DRY_RUN would delete {snap_id} of {volume_id} ({size} GiB) in {region}")
                deleted.append((region, snap_id, volume_id, size))
                continue
            try:
                ec2.delete_snapshot(SnapshotId=snap_id)
            except ClientError as e:
                # E.g. InvalidSnapshot.InUse when an AMI references it. Record
                # and keep sweeping.
                logger.error(f"Failed to delete {snap_id} in {region}: {e}")
                skipped.append((region, snap_id, f"error:{e.response.get('Error', {}).get('Code', 'ClientError')}"))
                continue
            logger.info(f"Deleted {snap_id} of {volume_id} ({size} GiB) in {region}")
            deleted.append((region, snap_id, volume_id, size))


def lambda_handler(event: dict[str, Any] | None, context: Any) -> dict[str, Any]:
    dry_run: bool = os.environ.get("DRY_RUN", "true").lower() == "true"
    retention_days: int = _retention_days()
    keep_count: int = _keep_count()
    cutoff: datetime = datetime.now(timezone.utc) - timedelta(days=retention_days)

    deleted: list[DeletedRow] = []
    skipped: list[SkippedRow] = []

    for region in _regions():
        try:
            _process_region(region, cutoff, retention_days, keep_count, dry_run, deleted, skipped)
        except Exception as e:  # noqa: BLE001 - one broken region must not abort the sweep
            logger.error(f"Region {region} error: {e}")
            continue

    logger.info(
        f"Summary: {len(deleted)} deleted, {len(skipped)} skipped "
        f"(DRY_RUN={dry_run}, RETENTION_DAYS={retention_days}, KEEP_COUNT={keep_count})"
    )
    return {"deleted": deleted, "skipped": skipped, "dry_run": dry_run}
