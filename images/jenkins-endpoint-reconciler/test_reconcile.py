"""Unit tests for discover_master_ip's probe-gated endpoint selection,
the X-Jenkins identity probe, and the EndpointSlice body's managed-by
contract labels.

Runs in the ci.yml lambda-pytest job. No kubernetes dependency: the
module-level `from kubernetes import ...` in reconcile.py is stubbed
before import; boto3 comes from the lambda test requirements and is
mocked per test.
"""
import datetime
import email.message
import http.server
import importlib.util
import os
import sys
import threading
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


def _headers(d):
    """Real case-insensitive header object, as urllib responses carry."""
    msg = email.message.Message()
    for k, v in d.items():
        msg[k] = v
    return msg


class _Resp:
    """Minimal opener context-manager response."""

    def __init__(self, headers):
        self.headers = _headers(headers)

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


def _http_error(code, msg, headers):
    return reconcile.urllib.error.HTTPError(
        "http://10.0.0.1:8080/login", code, msg, _headers(headers), None
    )


def _probe(open_effect):
    with mock.patch.object(reconcile._opener, "open", side_effect=open_effect):
        return reconcile._serves_jenkins("10.0.0.1", 8080)


def test_probe_accepts_master_with_x_jenkins_header():
    assert _probe(lambda *a, **k: _Resp({"X-Jenkins": "2.541.3"})) is True


def test_probe_accepts_lowercase_header_lookup():
    # Header lookup must be case-insensitive, as real HTTP servers may
    # downcase header names.
    assert _probe(lambda *a, **k: _Resp({"x-jenkins": "2.541.3"})) is True


def test_probe_rejects_impostor_without_x_jenkins_header():
    # A worker or cancelled-spot-fleet ghost sharing the master's
    # iit-billing-tag may listen on the port; a generic HTTP listener
    # answers 200 but never with the X-Jenkins header.
    assert _probe(lambda *a, **k: _Resp({})) is False


def test_probe_accepts_auth_restricted_master_via_http_error():
    # Jenkins sends X-Jenkins on error responses too; a 403 from an
    # auth-restricted master must still count as serving.
    assert _probe(_http_error(403, "Forbidden", {"X-Jenkins": "2.541.3"})) is True


def test_probe_rejects_http_error_without_x_jenkins_header():
    assert _probe(_http_error(404, "Not Found", {})) is False


def test_probe_judges_redirect_response_itself_not_target():
    # Redirects are not followed: a listener 302-ing to a real master must
    # be judged on its own (header-less) 3xx response and be rejected.
    assert _probe(_http_error(302, "Found", {"Location": "http://real:8080"})) is False
    # A genuine Jenkins redirect still carries X-Jenkins on the 3xx itself.
    assert _probe(_http_error(302, "Found", {"X-Jenkins": "2.541.3"})) is True


def _local_server(handler_cls):
    srv = http.server.HTTPServer(("127.0.0.1", 0), handler_cls)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    return srv


def test_real_opener_does_not_follow_redirects():
    # No mocks: drives reconcile's actual _opener against real local
    # servers, so this fails if _NoRedirect is ever dropped from the
    # opener construction (a mocked _opener.open cannot catch that).
    class Target(http.server.BaseHTTPRequestHandler):
        def do_GET(self):
            self.send_response(200)
            self.send_header("X-Jenkins", "2.541.3")
            self.end_headers()

        def log_message(self, *args):
            pass

    target = _local_server(Target)
    target_port = target.server_address[1]

    class Redirector(http.server.BaseHTTPRequestHandler):
        def do_GET(self):
            self.send_response(302)
            self.send_header("Location", f"http://127.0.0.1:{target_port}/login")
            self.end_headers()

        def log_message(self, *args):
            pass

    redirector = _local_server(Redirector)
    try:
        # Following the 302 would land on the X-Jenkins target and wrongly
        # accept; the no-redirect opener must reject the 302 itself.
        assert reconcile._serves_jenkins("127.0.0.1", redirector.server_address[1]) is False
        assert reconcile._serves_jenkins("127.0.0.1", target_port) is True
    finally:
        redirector.shutdown()
        target.shutdown()


def test_probe_rejects_non_http_listener():
    # Binary garbage on the port raises HTTPException (e.g. BadStatusLine),
    # which must reject the candidate, not crash the host reconcile.
    assert _probe(reconcile.http.client.BadStatusLine("\x00\x01")) is False


def test_probe_rejects_connection_failure():
    assert _probe(OSError("connection refused")) is False


def test_probe_rejects_url_error():
    # URLError wraps DNS failures and refused connections; it is an
    # OSError subclass and must not escape the probe.
    assert _probe(reconcile.urllib.error.URLError("refused")) is False
