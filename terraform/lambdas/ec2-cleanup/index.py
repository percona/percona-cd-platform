"""Reaper of untagged EC2 instances, with orphan EKS-stack cleanup, across all
regions.

Behavior of record: PS-10899 (Percona-Lab/jenkins-pipelines LambdaEC2Cleanup).
For every region it:

* skips EKS-managed instances whose cluster has a valid billing tag (instance
  tag, or the ``eksctl-<cluster>-cluster`` CloudFormation stack tag) or matches
  ``EKS_SKIP_PATTERN``; an EKS instance with no billing tag marks its
  ``eksctl-<cluster>-cluster`` stack for deletion (it does NOT enumerate
  standalone orphan stacks),
* terminates instances older than 600s that lack a valid ``iit-billing-tag``.

The original Lambda also auto-tagged Cirrus CI instances to protect them;
Cirrus CI shut down on 2026-06-01, so that path was dropped: a leftover
``CIRRUS_CI``-tagged instance is now reaped like any other untagged one.

``iit-billing-tag`` is treated as a unix-epoch expiry when numeric (a future
value keeps the instance), otherwise as a permanent category. IAM therefore
cannot scope ``TerminateInstances`` by tag (it can't compare a tag to the
clock); the execution policy scopes it to account instance ARNs instead.

Changes vs the inline CloudFormation version:

* env (``DRY_RUN``, ``EKS_SKIP_PATTERN``, ``REGIONS``) is read at invocation,
  not import, so a Terraform ``DRY_RUN`` flip is a plain apply and tests are
  deterministic,
* no module-level mutable cluster accumulator (warm containers reused it); it is
  threaded through the call,
* returns structured data instead of only logging.

Environment:

* ``DRY_RUN``          -- ``"true"``/``"false"`` (default ``"true"``: safe)
* ``EKS_SKIP_PATTERN`` -- regex of cluster names to spare (default ``"pe-.*"``)
* ``REGIONS``          -- CSV to scan; default = every region from
  ``describe_regions()`` (the CSV also lets tests bound the scan)
"""

from __future__ import annotations

import datetime
import logging
import os
import re
from typing import Any

import boto3
from botocore.exceptions import ClientError

logger = logging.getLogger()
logger.setLevel("INFO")

TerminatedInfo = dict[str, Any]
SkippedInfo = dict[str, str]


def _dry_run() -> bool:
    return os.environ.get("DRY_RUN", "true").lower() == "true"


def _eks_skip_pattern() -> str:
    return os.environ.get("EKS_SKIP_PATTERN", "pe-.*")


def _regions() -> list[str]:
    raw = os.environ.get("REGIONS", "").strip()
    if raw:
        return [r.strip() for r in raw.split(",") if r.strip()]
    return [r["RegionName"] for r in boto3.client("ec2").describe_regions()["Regions"]]


def convert_tags_to_dict(tags: list[dict[str, str]] | None) -> dict[str, str]:
    return {tag["Key"]: tag["Value"] for tag in tags} if tags else {}


def get_eks_cluster_name(tags_dict: dict[str, str]) -> str | None:
    for key in ("aws:eks:cluster-name", "eks:eks-cluster-name"):
        if key in tags_dict:
            return tags_dict[key]
    for key in tags_dict:
        if key.startswith("kubernetes.io/cluster/"):
            return key.replace("kubernetes.io/cluster/", "")
    return None


def has_valid_billing_tag(tags_dict: dict[str, str]) -> bool:
    """True if iit-billing-tag is a non-numeric category or a future epoch."""
    tag_value = tags_dict.get("iit-billing-tag")
    if not tag_value:
        return False
    try:
        exp = int(tag_value)
    except ValueError:
        logger.info(f"Valid billing tag category: {tag_value}")
        return True
    now = int(datetime.datetime.now(datetime.timezone.utc).timestamp())
    if exp > now:
        logger.info(f"Valid billing tag expiration {exp} (expires in {exp - now}s)")
        return True
    logger.info(f"Billing tag expired: {exp} < {now} (expired {now - exp}s ago)")
    return False


def eks_stack_has_valid_billing(cluster_name: str | None, region: str) -> bool:
    """Whether the cluster's eksctl-<name>-cluster stack protects it from
    deletion. FAILS CLOSED: any error determining the stack's tags
    (AccessDenied, throttling, transient) returns True (protect). Only a
    confirmed-missing stack, or a stack with a confirmed missing/expired billing
    tag, returns False -- so a transient CFN error can never get a tagged
    cluster's stack deleted."""
    if not cluster_name:
        return False
    stack_name = f"eksctl-{cluster_name}-cluster"
    try:
        cfn = boto3.client("cloudformation", region_name=region)
        resp = cfn.describe_stacks(StackName=stack_name)
        tags = {t["Key"]: t["Value"] for t in resp["Stacks"][0].get("Tags", [])}
        # Run through the same epoch/category check so an EXPIRED numeric tag
        # does not protect the stack forever.
        return has_valid_billing_tag(tags)
    except ClientError as e:
        if "does not exist" in str(e):
            return False  # no stack -> no CFN-level protection
        logger.error(f"CF tag check error for {stack_name} in {region}: {e}; failing closed (protect)")
        return True
    except Exception as e:  # noqa: BLE001 - any uncertainty must protect, never reap
        logger.error(f"Unexpected CF error for {stack_name} in {region}: {e}; failing closed (protect)")
        return True


def is_eks_managed(
    instance: Any,
    region: str,
    eks_skip_pattern: str,
    clusters_to_delete: dict[str, set[str]],
) -> bool:
    tags_dict = convert_tags_to_dict(instance.tags)
    indicators = (
        "kubernetes.io/cluster/", "aws:eks:cluster-name",
        "eks:eks-cluster-name", "eks:kubernetes-node-pool-name",
        "aws:ec2:managed-launch",
    )
    if not any(ind in key for key in tags_dict for ind in indicators):
        return False

    cluster_name = get_eks_cluster_name(tags_dict)
    if has_valid_billing_tag(tags_dict):
        logger.info(f"{instance.id} EKS ({cluster_name}), has billing tag, skip")
        return True
    if eks_stack_has_valid_billing(cluster_name, region):
        logger.info(f"{instance.id} EKS ({cluster_name}), stack billing tag valid or undeterminable, skip")
        return True

    if cluster_name and eks_skip_pattern:
        try:
            if re.match(eks_skip_pattern, cluster_name):
                logger.info(f"{instance.id} EKS ({cluster_name}), matches skip pattern, skip")
            else:
                logger.info(f"{instance.id} EKS ({cluster_name}), no billing tag, mark stack for deletion")
                clusters_to_delete.setdefault(region, set()).add(cluster_name)
        except re.error as e:
            logger.error(f"Invalid regex '{eks_skip_pattern}': {e}, skipping all EKS")
    return True


def should_terminate(instance: Any) -> bool:
    tags_dict = convert_tags_to_dict(instance.tags)
    if has_valid_billing_tag(tags_dict):
        return False
    running = datetime.datetime.now(datetime.timezone.utc) - instance.launch_time
    return running.total_seconds() > 600


def get_name_tag(instance: Any) -> str | None:
    return convert_tags_to_dict(instance.tags).get("Name")


def cleanup_failed_stack(stack_name: str, region: str) -> bool:
    """Best-effort remediation of DELETE_FAILED resources so the retry can
    proceed: revoke leftover SG ingress, disassociate route tables, delete
    routes."""
    try:
        cfn = boto3.client("cloudformation", region_name=region)
        ec2 = boto3.client("ec2", region_name=region)
        events = cfn.describe_stack_events(StackName=stack_name)
        failed: dict[str, dict[str, Any]] = {}
        for ev in events["StackEvents"]:
            if ev.get("ResourceStatus") == "DELETE_FAILED":
                lid = ev["LogicalResourceId"]
                failed.setdefault(lid, {"Type": ev["ResourceType"], "PhysicalId": ev.get("PhysicalResourceId")})
        if not failed:
            return True
        logger.info(f"Cleaning {len(failed)} failed resources for {stack_name}")
        for res in failed.values():
            rtype, pid = res["Type"], res["PhysicalId"]
            try:
                if rtype == "AWS::EC2::SecurityGroupIngress" and pid:
                    sg_id = pid.split("|")[0] if "|" in pid else None
                    if sg_id and sg_id.startswith("sg-"):
                        r = ec2.describe_security_groups(GroupIds=[sg_id])
                        if r["SecurityGroups"] and r["SecurityGroups"][0]["IpPermissions"]:
                            ec2.revoke_security_group_ingress(
                                GroupId=sg_id,
                                IpPermissions=r["SecurityGroups"][0]["IpPermissions"],
                            )
                elif rtype == "AWS::EC2::SubnetRouteTableAssociation" and pid and pid.startswith("rtbassoc-"):
                    ec2.disassociate_route_table(AssociationId=pid)
                elif rtype == "AWS::EC2::Route" and pid:
                    parts = pid.split("_")
                    if len(parts) == 2 and parts[0].startswith("rtb-"):
                        ec2.delete_route(RouteTableId=parts[0], DestinationCidrBlock=parts[1])
            except ClientError as e:
                code = e.response.get("Error", {}).get("Code", "")
                if code not in ("InvalidGroup.NotFound", "InvalidAssociationID.NotFound", "InvalidRoute.NotFound"):
                    logger.warning(f"Cleanup {rtype} {pid}: {e}")
            except Exception as e:  # noqa: BLE001
                logger.warning(f"Cleanup error {rtype} {pid}: {e}")
        return True
    except Exception as e:  # noqa: BLE001
        logger.error(f"Failed stack cleanup {stack_name}: {e}")
        return False


def delete_eks_stack(cluster_name: str, region: str, dry_run: bool) -> bool:
    try:
        cfn = boto3.client("cloudformation", region_name=region)
        stack_name = f"eksctl-{cluster_name}-cluster"
        try:
            resp = cfn.describe_stacks(StackName=stack_name)
            stack = resp["Stacks"][0]
            status = stack["StackStatus"]
        except ClientError as e:
            if "does not exist" in str(e):
                logger.warning(f"CF stack {stack_name} not found in {region}")
                return False
            raise
        # TOCTOU guard: a valid billing tag may have been added between the scan
        # (is_eks_managed) and now. Re-read the stack tags immediately before
        # deleting and abort if it is now protected.
        tags = {t["Key"]: t["Value"] for t in stack.get("Tags", [])}
        if has_valid_billing_tag(tags):
            logger.info(f"{stack_name} acquired a valid billing tag since scan; skipping deletion")
            return False
        if status == "DELETE_FAILED":
            if dry_run:
                logger.info(f"DRY_RUN: would retry deletion of {stack_name}")
            else:
                cleanup_failed_stack(stack_name, region)
                cfn.delete_stack(StackName=stack_name)
                logger.info(f"Retrying deletion of {stack_name}")
            return True
        if "DELETE" in status and status != "DELETE_COMPLETE":
            logger.info(f"Stack {stack_name} already deleting ({status})")
            return True
        if dry_run:
            logger.info(f"DRY_RUN: would delete CF stack {stack_name} for {cluster_name} in {region}")
        else:
            cfn.delete_stack(StackName=stack_name)
            logger.info(f"Deleted CF stack {stack_name} for {cluster_name} in {region}")
        return True
    except ClientError as e:
        logger.error(f"CF delete failed for {cluster_name} in {region}: {e}")
        return False
    except Exception as e:  # noqa: BLE001
        logger.error(f"Unexpected error deleting {cluster_name} in {region}: {e}")
        return False


def process_region(
    region: str,
    dry_run: bool,
    eks_skip_pattern: str,
    clusters_to_delete: dict[str, set[str]],
) -> tuple[list[TerminatedInfo], list[SkippedInfo]]:
    ec2 = boto3.resource("ec2", region_name=region)
    instances = ec2.instances.filter(
        Filters=[{"Name": "instance-state-name", "Values": ["running"]}]
    )
    terminated: list[TerminatedInfo] = []
    skipped: list[SkippedInfo] = []

    for instance in instances:
        try:
            if is_eks_managed(instance, region, eks_skip_pattern, clusters_to_delete):
                cluster = get_eks_cluster_name(convert_tags_to_dict(instance.tags))
                skipped.append({"id": instance.id, "reason": f"EKS ({cluster or 'unknown'})"})
                continue

            if should_terminate(instance):
                info: TerminatedInfo = {
                    "InstanceId": instance.id,
                    "SSHKeyName": instance.key_name,
                    "NameTag": get_name_tag(instance),
                    "AvailabilityZone": instance.placement["AvailabilityZone"],
                    "Region": region,
                }
                if dry_run:
                    # Report it in `terminated` (the action list), same convention as the
                    # volume reaper's `deleted`; the dry_run flag tells the caller nothing
                    # was actually executed. Do NOT also list it in `skipped`.
                    logger.info(f"DRY_RUN: would terminate {instance.id} (Name: {info['NameTag']})")
                    terminated.append(info)
                else:
                    try:
                        instance.terminate()
                        terminated.append(info)
                        logger.info(f"Terminated {instance.id} in {region}")
                    except ClientError as e:
                        logger.error(f"Failed to terminate {instance.id}: {e}")
                        skipped.append({"id": instance.id, "reason": f"Error: {e}"})
        except Exception as e:  # noqa: BLE001 - one bad instance must not abort the region
            logger.error(f"Error processing {instance.id} in {region}: {e}")
            continue

    if skipped:
        logger.info(f"Skipped {len(skipped)} in {region}")
    return terminated, skipped


def lambda_handler(event: dict[str, Any] | None, context: Any) -> dict[str, Any]:
    dry_run = _dry_run()
    eks_skip_pattern = _eks_skip_pattern()
    clusters_to_delete: dict[str, set[str]] = {}

    all_terminated: list[TerminatedInfo] = []
    all_skipped: list[SkippedInfo] = []
    deleted_clusters: list[str] = []

    for region in _regions():
        try:
            term, skip = process_region(region, dry_run, eks_skip_pattern, clusters_to_delete)
            all_terminated.extend(term)
            all_skipped.extend(skip)
        except Exception as e:  # noqa: BLE001
            logger.error(f"Region {region} error: {e}")
            continue

    for region, clusters in clusters_to_delete.items():
        for name in clusters:
            try:
                if delete_eks_stack(name, region, dry_run):
                    deleted_clusters.append(f"{name} ({region})")
            except Exception as e:  # noqa: BLE001
                logger.error(f"Cluster delete {name} in {region}: {e}")

    logger.info(
        f"Summary: {len(all_terminated)} terminated, {len(deleted_clusters)} EKS stacks, "
        f"{len(all_skipped)} skipped (DRY_RUN={dry_run})"
    )
    return {
        "terminated": all_terminated,
        "deleted_clusters": deleted_clusters,
        "skipped": all_skipped,
        "dry_run": dry_run,
    }
