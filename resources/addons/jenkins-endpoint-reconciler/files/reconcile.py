#!/usr/bin/env python3
"""Reconcile EC2 Jenkins master IPs into K8s EndpointSlices.

For each host in HOSTS_JSON, this script:
  1. Calls ec2:DescribeInstances tag-filtered by `iit-billing-tag=<tag>`
     and `instance-state-name=running` in the host's region.
  2. Writes an EndpointSlice `jenkins-<name>` in TARGET_NAMESPACE pointing
     at the discovered private IP. The matching ClusterIP Service (managed
     by the same Helm chart) selectors nothing, so the EndpointSlice is
     the only way pods are advertised — the reconciler is its sole writer.

Designed to run on a 1-minute CronJob. Exits non-zero only on real errors
(boto3 / kubernetes API failures). An empty discovery (no running master)
is a valid steady state during SpotFleet replacement; the EndpointSlice is
written with `endpoints: []` and the proxy serves its 503 page until the
next run picks the new IP up.
"""
from __future__ import annotations

import json
import logging
import os
import sys

import boto3
from kubernetes import client, config

log = logging.getLogger("reconcile")
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s %(message)s",
)

TARGET_NAMESPACE = os.environ.get("TARGET_NAMESPACE", "jenkins-ingress")
HOSTS = json.loads(os.environ["HOSTS_JSON"])
MANAGED_BY = "jenkins-endpoint-reconciler"


def discover_master_ip(region: str, tag_value: str) -> str | None:
    """Return the private IP of the single running master matching the tag,
    or None if no running instance exists."""
    ec2 = boto3.client("ec2", region_name=region)
    resp = ec2.describe_instances(
        Filters=[
            {"Name": "tag:iit-billing-tag", "Values": [tag_value]},
            {"Name": "instance-state-name", "Values": ["running"]},
        ]
    )
    ips = [
        i["PrivateIpAddress"]
        for r in resp["Reservations"]
        for i in r["Instances"]
        if "PrivateIpAddress" in i
    ]
    if len(ips) > 1:
        log.warning(
            "multiple running masters for tag=%s in %s: %s — picking first",
            tag_value,
            region,
            ips,
        )
    return ips[0] if ips else None


def reconcile(host: dict) -> None:
    """Read or create the EndpointSlice for one host."""
    name = f"jenkins-{host['name']}"
    ip = discover_master_ip(host["region"], host["tag"])

    body = {
        "apiVersion": "discovery.k8s.io/v1",
        "kind": "EndpointSlice",
        "metadata": {
            "name": name,
            "namespace": TARGET_NAMESPACE,
            "labels": {
                "kubernetes.io/service-name": name,
                "app.kubernetes.io/managed-by": MANAGED_BY,
            },
        },
        "addressType": "IPv4",
        "ports": [
            {"port": host["port"], "protocol": "TCP", "name": "http"},
        ],
        "endpoints": (
            [{"addresses": [ip], "conditions": {"ready": True}}] if ip else []
        ),
    }

    api = client.DiscoveryV1Api()
    try:
        existing = api.read_namespaced_endpoint_slice(name, TARGET_NAMESPACE)
        current = (
            existing.endpoints[0].addresses[0]
            if existing.endpoints and existing.endpoints[0].addresses
            else None
        )
        if current == ip:
            log.info("%s: no change (ip=%s)", name, ip or "EMPTY")
            return
        api.replace_namespaced_endpoint_slice(name, TARGET_NAMESPACE, body)
        log.info("%s: %s -> %s", name, current or "EMPTY", ip or "EMPTY")
    except client.exceptions.ApiException as e:
        if e.status == 404:
            api.create_namespaced_endpoint_slice(TARGET_NAMESPACE, body)
            log.info("%s: CREATE -> %s", name, ip or "EMPTY")
        else:
            raise


def main() -> int:
    config.load_incluster_config()
    errors: list[str] = []
    for host in HOSTS:
        try:
            reconcile(host)
        except Exception as e:
            log.exception("%s: reconcile failed: %s", host["name"], e)
            errors.append(host["name"])
    if errors:
        log.error("hosts failed: %s", errors)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
