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
import socket
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


def _serves_jenkins(ip: str, port: int, timeout: float = 2.0) -> bool:
    """True if a TCP connection to ip:port succeeds (Jenkins is listening)."""
    try:
        with socket.create_connection((ip, int(port)), timeout=timeout):
            return True
    except OSError:
        return False


def discover_master_ip(region: str, tag_value: str, port: int) -> tuple[str | None, bool]:
    """Find the master's private IP for the tag. Returns (ip, write):

      (ip,   True)   write this IP into the EndpointSlice.
      (None, True)   no running instance -> write an empty slice (the master is
                     down; the proxy 503 page is the correct steady state during
                     a SpotFleet replacement).
      (None, False)  ambiguous: more than one instance carries the tag and we
                     cannot single out a serving master -> KEEP the existing
                     EndpointSlice, do NOT overwrite. A worker that shares the
                     master's iit-billing-tag, or a cancelled-spot-fleet ghost,
                     must never be able to blackhole the master's ingress.

    With >1 match, ips[0] is non-deterministic, so disambiguate by which instance
    actually serves Jenkins on :port, breaking ties toward the newest launch (a
    freshly replaced master over a lingering serving ghost). The health probe is
    the primary signal, not launch time, because a worker can be newer than the
    master.
    """
    ec2 = boto3.client("ec2", region_name=region)
    resp = ec2.describe_instances(
        Filters=[
            {"Name": "tag:iit-billing-tag", "Values": [tag_value]},
            {"Name": "instance-state-name", "Values": ["running"]},
        ]
    )
    insts = [
        (i["PrivateIpAddress"], i["LaunchTime"])
        for r in resp["Reservations"]
        for i in r["Instances"]
        if "PrivateIpAddress" in i
    ]
    if not insts:
        return None, True
    if len(insts) == 1:
        return insts[0][0], True

    live = [(ip, lt) for ip, lt in insts if _serves_jenkins(ip, port)]
    if not live:
        log.error(
            "tag=%s in %s matched %d instances (%s) but none serve :%s; "
            "leaving EndpointSlice unchanged",
            tag_value, region, len(insts), [ip for ip, _ in insts], port,
        )
        return None, False

    live.sort(key=lambda t: t[1], reverse=True)  # newest first
    chosen = live[0][0]
    log.warning(
        "tag=%s in %s matched %d instances; %d serve :%s; selected newest serving -> %s",
        tag_value, region, len(insts), len(live), port, chosen,
    )
    return chosen, True


def reconcile(host: dict) -> None:
    """Read or create the EndpointSlice for one host."""
    name = f"jenkins-{host['name']}"
    ip, write = discover_master_ip(host["region"], host["tag"], host["port"])
    if not write:
        log.info("%s: keep (ambiguous discovery; existing EndpointSlice left intact)", name)
        return

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

    # Always replace; skip the read-and-compare. The kubernetes python
    # client deserialises EndpointSlice via a typed model whose `endpoints`
    # field rejects None — but the API server omits the field entirely
    # when the slice has no endpoints, so reads of an empty slice raise
    # `ValueError: Invalid value for endpoints, must not be None` before
    # we can even check it. Replace is idempotent and the resourceVersion
    # bump is irrelevant for a 1/min job.
    api = client.DiscoveryV1Api()
    try:
        api.replace_namespaced_endpoint_slice(name, TARGET_NAMESPACE, body)
        log.info("%s: write -> %s", name, ip or "EMPTY")
    except client.exceptions.ApiException as e:
        if e.status == 404:
            api.create_namespaced_endpoint_slice(TARGET_NAMESPACE, body)
            log.info("%s: create -> %s", name, ip or "EMPTY")
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
