"""Daily reaper of unattached (``available``) EBS volumes across all regions.

Behavior of record: PS-11262 (Percona-Lab/jenkins-pipelines PR #4178). The
previous CloudFormation version filtered on ``tag:Name=*`` and so silently
skipped untagged volumes (two untagged 30 TB gp3 orphans ran for days); it also
crashed on volumes with no tags. This port:

* evaluates every ``available`` volume regardless of tags,
* tolerates volumes whose tag set is empty/``None``,
* honors a ``MIN_AGE_HOURS`` floor so a freshly-created volume survives a build,
* keeps the opt-outs: a ``PerconaKeep`` tag, or ``do not remove`` in the Name
  (case-insensitive),
* gates the actual delete behind ``DRY_RUN``,
* isolates failures: one undeletable volume (e.g. the available->in-use race
  raising ``VolumeInUse``) or one broken region never aborts the sweep,
* returns structured data instead of only logging.

Environment (read at invocation, so a Terraform ``DRY_RUN`` flip is a plain
apply, and tests can pin values deterministically):

* ``DRY_RUN``        -- ``"true"``/``"false"`` (default ``"true"``: safe; the
  Terraform instantiation sets it explicitly)
* ``MIN_AGE_HOURS``  -- int (default ``24``; non-numeric values fall back to 24)
* ``REGIONS``        -- CSV of regions (default the 8-region list below; also
  lets tests bound the scan to one region)
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

DEFAULT_REGIONS: list[str] = [
    "us-east-1", "us-east-2", "us-west-1", "us-west-2",
    "eu-central-1", "eu-west-1", "eu-west-2", "eu-west-3",
]
KEEP_TAG: str = "PerconaKeep"
KEEP_NAME_SUBSTR: str = "do not remove"
DEFAULT_MIN_AGE_HOURS: int = 24

# (region, volume_id, size_gib)
DeletedRow = tuple[str, str, int]
# (region, volume_id, reason)
SkippedRow = tuple[str, str, str]


def _regions() -> list[str]:
    raw = os.environ.get("REGIONS", "").strip()
    return [r.strip() for r in raw.split(",") if r.strip()] or DEFAULT_REGIONS


def _min_age_hours() -> int:
    raw = os.environ.get("MIN_AGE_HOURS", str(DEFAULT_MIN_AGE_HOURS))
    try:
        return int(raw)
    except ValueError:
        logger.error(f"Invalid MIN_AGE_HOURS '{raw}'; falling back to {DEFAULT_MIN_AGE_HOURS}")
        return DEFAULT_MIN_AGE_HOURS


def _tags(volume: Any) -> dict[str, str]:
    # volume.tags is None when the volume has no tags at all.
    return {t["Key"]: t.get("Value", "") for t in (volume.tags or [])}


def _process_region(
    region: str,
    cutoff: datetime,
    min_age_hours: int,
    dry_run: bool,
    deleted: list[DeletedRow],
    skipped: list[SkippedRow],
) -> None:
    ec2 = boto3.resource("ec2", region_name=region)
    # Evaluate ALL unattached volumes, not only those carrying a Name tag.
    for volume in ec2.volumes.filter(Filters=[{"Name": "status", "Values": ["available"]}]):
        tags = _tags(volume)
        # Read identifying attributes up front: once volume.delete() runs the
        # resource can no longer be lazily reloaded, so reading volume.size
        # afterwards would raise InvalidVolume.NotFound.
        vol_id, vol_size = volume.id, volume.size
        if KEEP_TAG in tags:
            skipped.append((region, vol_id, "PerconaKeep"))
            continue
        if KEEP_NAME_SUBSTR in tags.get("Name", "").lower():
            skipped.append((region, vol_id, "name:do-not-remove"))
            continue
        if volume.create_time > cutoff:
            skipped.append((region, vol_id, f"younger-than-{min_age_hours}h"))
            continue
        if dry_run:
            logger.info(f"DRY_RUN would delete {vol_id} ({vol_size} GiB) in {region}")
            deleted.append((region, vol_id, vol_size))
            continue
        try:
            volume.delete()
        except ClientError as e:
            # E.g. VolumeInUse when the volume got attached between the
            # describe and the delete. Record and keep sweeping.
            logger.error(f"Failed to delete {vol_id} in {region}: {e}")
            skipped.append((region, vol_id, f"error:{e.response.get('Error', {}).get('Code', 'ClientError')}"))
            continue
        logger.info(f"Deleted {vol_id} ({vol_size} GiB) in {region}")
        deleted.append((region, vol_id, vol_size))


def lambda_handler(event: dict[str, Any] | None, context: Any) -> dict[str, Any]:
    dry_run: bool = os.environ.get("DRY_RUN", "true").lower() == "true"
    min_age_hours: int = _min_age_hours()
    cutoff: datetime = datetime.now(timezone.utc) - timedelta(hours=min_age_hours)

    deleted: list[DeletedRow] = []
    skipped: list[SkippedRow] = []

    for region in _regions():
        try:
            _process_region(region, cutoff, min_age_hours, dry_run, deleted, skipped)
        except Exception as e:  # noqa: BLE001 - one broken region must not abort the sweep
            logger.error(f"Region {region} error: {e}")
            continue

    logger.info(
        f"Summary: {len(deleted)} deleted, {len(skipped)} skipped "
        f"(DRY_RUN={dry_run}, MIN_AGE_HOURS={min_age_hours})"
    )
    return {"deleted": deleted, "skipped": skipped, "dry_run": dry_run}
