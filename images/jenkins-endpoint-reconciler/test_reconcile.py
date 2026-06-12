"""Unit tests for discover_master_ip's probe-gated endpoint selection
and the EndpointSlice body's managed-by contract labels.

Runs in the ci.yml lambda-pytest job. No kubernetes dependency: the
module-level `from kubernetes import ...` in reconcile.py is stubbed
before import; boto3 comes from the lambda test requirements and is
mocked per test.
"""
import datetime
import importlib.util
import os
import sys
import types
from pathlib import Path
from unittest import mock

os.environ.setdefault("HOSTS_JSON", "[]")

# Stub the kubernetes client so importing reconcile.py needs no cluster deps.
_k8s = types.ModuleType("kubernetes")
_k8s.client = mock.MagicMock()
_k8s.config = mock.MagicMock()
sys.modules.setdefault("kubernetes", _k8s)

_spec = importlib.util.spec_from_file_location(
    "reconcile", Path(__file__).with_name("reconcile.py")
)
reconcile = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(reconcile)


def _instances(*specs):
    """describe_instances response from (ip, launch_offset_minutes) pairs."""
    base = datetime.datetime(2026, 6, 11, 0, 0, 0)
    return {
        "Reservations": [
            {
                "Instances": [
                    {
                        "PrivateIpAddress": ip,
                        "LaunchTime": base + datetime.timedelta(minutes=off),
                    }
                    for ip, off in specs
                ]
            }
        ]
    }


def _discover(resp, serving):
    with mock.patch.object(reconcile, "boto3") as b3, mock.patch.object(
        reconcile, "_serves_jenkins", side_effect=lambda ip, port: ip in serving
    ):
        b3.client.return_value.describe_instances.return_value = resp
        return reconcile.discover_master_ip("us-east-1", "jenkins-x", 8080)


def test_no_instances_writes_empty_slice():
    assert _discover(_instances(), set()) == (None, True)


def test_single_serving_is_written():
    assert _discover(_instances(("10.0.0.1", 0)), {"10.0.0.1"}) == ("10.0.0.1", True)


def test_single_not_serving_keeps_existing_slice():
    # A sole just-launched instance must not receive traffic before Jenkins
    # listens, and a probe blip against a healthy master must not wipe the
    # slice it is already in.
    assert _discover(_instances(("10.0.0.2", 0)), set()) == (None, False)


def test_multi_newest_serving_wins():
    resp = _instances(("10.0.0.1", 0), ("10.0.0.2", 5))
    assert _discover(resp, {"10.0.0.1", "10.0.0.2"}) == ("10.0.0.2", True)


def test_multi_serving_beats_newer_non_serving():
    # A worker can be newer than the master; the probe is the primary signal.
    resp = _instances(("10.0.0.1", 0), ("10.0.0.9", 30))
    assert _discover(resp, {"10.0.0.1"}) == ("10.0.0.1", True)


def test_multi_none_serving_keeps_existing_slice():
    resp = _instances(("10.0.0.1", 0), ("10.0.0.2", 5))
    assert _discover(resp, set()) == (None, False)


def test_written_slice_carries_managed_by_contract_labels():
    # The k8s EndpointSlice contract: manually managed slices must set a
    # unique endpointslice.kubernetes.io/managed-by so other controllers
    # know not to touch them.
    host = {"name": "x", "region": "us-east-1", "tag": "jenkins-x", "port": 8080}
    api = reconcile.client.DiscoveryV1Api.return_value
    api.replace_namespaced_endpoint_slice.reset_mock()
    with mock.patch.object(
        reconcile, "discover_master_ip", return_value=("10.0.0.1", True)
    ):
        reconcile.reconcile(host)
    body = api.replace_namespaced_endpoint_slice.call_args.args[2]
    assert body["metadata"]["labels"] == {
        "kubernetes.io/service-name": "jenkins-x",
        "app.kubernetes.io/managed-by": "jenkins-endpoint-reconciler",
        "endpointslice.kubernetes.io/managed-by": "jenkins-endpoint-reconciler",
    }
