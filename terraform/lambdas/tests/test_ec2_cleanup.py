"""Regression tests for the ec2-cleanup reaper: DRY_RUN gates every destructive
action, billing tag is read by key and interpreted as a unix-epoch expiry, and
EKS instances without a billing tag mark their eksctl stack for deletion unless
they match the skip pattern. Cirrus CI shut down 2026-06-01, so a leftover
CIRRUS_CI-tagged instance must be reaped like any other untagged one.
"""

from __future__ import annotations

import datetime as dt
from types import SimpleNamespace

import boto3
import pytest
from freezegun import freeze_time
from moto import mock_aws

pytestmark = [pytest.mark.aws]

REGION = "us-east-1"
AMI = "ami-12c6146b"
# Run the reaper far in the future so any instance is comfortably older than the
# 600s termination threshold, regardless of how moto stamps launch_time.
FUTURE = "2030-01-01 00:00:00"


def _mk_instance(region, *, tags=None):
    ec2 = boto3.resource("ec2", region_name=region)
    inst = ec2.create_instances(ImageId=AMI, MinCount=1, MaxCount=1)[0]
    if tags:
        inst.create_tags(Tags=[{"Key": k, "Value": v} for k, v in tags.items()])
    return inst.id


def _state(region, iid):
    return boto3.resource("ec2", region_name=region).Instance(iid).state["Name"]


def _tags(region, iid):
    inst = boto3.resource("ec2", region_name=region).Instance(iid)
    return {t["Key"]: t["Value"] for t in (inst.tags or [])}


# Minimal valid CFN template for moto to stand up an eksctl-<cluster>-cluster stack.
CFN_TEMPLATE = '{"Resources": {"T": {"Type": "AWS::SNS::Topic"}}}'
PAST_EPOCH = "1700000000"  # 2023-11; always expired relative to FUTURE (2030)


def _mk_eksctl_stack(region, cluster, *, tags=None):
    cfn = boto3.client("cloudformation", region_name=region)
    kwargs = {"StackName": f"eksctl-{cluster}-cluster", "TemplateBody": CFN_TEMPLATE}
    if tags:
        kwargs["Tags"] = [{"Key": k, "Value": v} for k, v in tags.items()]
    cfn.create_stack(**kwargs)


# ---- handler-level (moto) ----

def test_untagged_old_instance_terminated(ec2_cleanup, monkeypatch):
    monkeypatch.setenv("DRY_RUN", "false")
    monkeypatch.setenv("REGIONS", REGION)
    with mock_aws():
        iid = _mk_instance(REGION)  # untagged
        with freeze_time(FUTURE):
            result = ec2_cleanup.lambda_handler({}, None)
        assert iid in [t["InstanceId"] for t in result["terminated"]]
        assert _state(REGION, iid) == "terminated"


def test_dry_run_does_not_terminate(ec2_cleanup, monkeypatch):
    monkeypatch.setenv("DRY_RUN", "true")
    monkeypatch.setenv("REGIONS", REGION)
    with mock_aws():
        iid = _mk_instance(REGION)
        with freeze_time(FUTURE):
            result = ec2_cleanup.lambda_handler({}, None)
        assert result["dry_run"] is True
        assert iid in [t["InstanceId"] for t in result["terminated"]]  # reported as would-terminate
        assert iid not in [s["id"] for s in result["skipped"]]          # not double-listed
        assert _state(REGION, iid) == "running"  # gated: not actually terminated


def test_billing_category_tag_spares_instance(ec2_cleanup, monkeypatch):
    monkeypatch.setenv("DRY_RUN", "false")
    monkeypatch.setenv("REGIONS", REGION)
    with mock_aws():
        iid = _mk_instance(REGION, tags={"iit-billing-tag": "jenkins"})
        with freeze_time(FUTURE):
            ec2_cleanup.lambda_handler({}, None)
        assert _state(REGION, iid) == "running"


def test_leftover_cirrus_instance_is_reaped(ec2_cleanup, monkeypatch):
    """Cirrus CI shut down 2026-06-01: a straggler CIRRUS_CI instance must NOT
    be auto-tagged/protected anymore; it is reaped like any untagged one."""
    monkeypatch.setenv("DRY_RUN", "false")
    monkeypatch.setenv("REGIONS", REGION)
    with mock_aws():
        iid = _mk_instance(REGION, tags={"CIRRUS_CI": "true"})
        with freeze_time(FUTURE):
            ec2_cleanup.lambda_handler({}, None)
        assert _state(REGION, iid) == "terminated"
        assert _tags(REGION, iid).get("iit-billing-tag") != "CirrusCI"


def test_dry_run_unset_defaults_safe(ec2_cleanup, monkeypatch):
    """With DRY_RUN absent from the environment, the reaper must not act."""
    monkeypatch.setenv("REGIONS", REGION)  # DRY_RUN deliberately NOT set
    with mock_aws():
        iid = _mk_instance(REGION)
        with freeze_time(FUTURE):
            result = ec2_cleanup.lambda_handler({}, None)
        assert result["dry_run"] is True
        assert _state(REGION, iid) == "running"


# ---- unit-level billing-tag + EKS branch logic ----

def test_has_valid_billing_tag_variants(ec2_cleanup):
    f = ec2_cleanup.has_valid_billing_tag
    now = int(dt.datetime.now(dt.timezone.utc).timestamp())
    assert f({"iit-billing-tag": "jenkins"}) is True        # non-numeric category
    assert f({}) is False                                   # missing
    assert f({"iit-billing-tag": ""}) is False              # empty
    assert f({"iit-billing-tag": str(now + 3600)}) is True  # future epoch
    assert f({"iit-billing-tag": str(now - 3600)}) is False  # expired epoch


def test_should_terminate_logic(ec2_cleanup):
    old = dt.datetime.now(dt.timezone.utc) - dt.timedelta(hours=2)
    recent = dt.datetime.now(dt.timezone.utc) - dt.timedelta(seconds=60)
    assert ec2_cleanup.should_terminate(SimpleNamespace(tags=[], launch_time=old)) is True
    assert ec2_cleanup.should_terminate(SimpleNamespace(tags=[], launch_time=recent)) is False
    spared = SimpleNamespace(tags=[{"Key": "iit-billing-tag", "Value": "jenkins"}], launch_time=old)
    assert ec2_cleanup.should_terminate(spared) is False


def test_eks_no_billing_marks_cluster_for_deletion(ec2_cleanup):
    with mock_aws():
        fake = SimpleNamespace(id="i-1", tags=[{"Key": "kubernetes.io/cluster/orphan", "Value": "owned"}])
        clusters_to_delete: dict[str, set[str]] = {}
        assert ec2_cleanup.is_eks_managed(fake, REGION, "pe-.*", clusters_to_delete) is True
        assert clusters_to_delete == {REGION: {"orphan"}}


def test_eks_skip_pattern_match_not_marked(ec2_cleanup):
    with mock_aws():
        fake = SimpleNamespace(id="i-2", tags=[{"Key": "kubernetes.io/cluster/pe-prod", "Value": "owned"}])
        clusters_to_delete: dict[str, set[str]] = {}
        assert ec2_cleanup.is_eks_managed(fake, REGION, "pe-.*", clusters_to_delete) is True
        assert clusters_to_delete == {}


# ---- molecule age-bound (leaked package-testing instances) ----

def test_should_terminate_molecule_age_bound(ec2_cleanup):
    import re as _re
    mol_re = _re.compile(r".*_package_testing$")
    old = dt.datetime.now(dt.timezone.utc) - dt.timedelta(hours=8)
    young = dt.datetime.now(dt.timezone.utc) - dt.timedelta(hours=1)
    tag = [{"Key": "iit-billing-tag", "Value": "ps_80_package_testing"}]
    # Past the bound: reaped despite the (category) billing tag.
    assert ec2_cleanup.should_terminate(
        SimpleNamespace(id="i-old", tags=tag, launch_time=old), mol_re, 7 * 3600) is True
    # Within the bound: spared.
    assert ec2_cleanup.should_terminate(
        SimpleNamespace(id="i-young", tags=tag, launch_time=young), mol_re, 7 * 3600) is False
    # Non-matching category tag: permanent exemption unchanged.
    other = [{"Key": "iit-billing-tag", "Value": "jenkins"}]
    assert ec2_cleanup.should_terminate(
        SimpleNamespace(id="i-cat", tags=other, launch_time=old), mol_re, 7 * 3600) is False
    # No pattern (disabled): molecule tag behaves as a plain category again.
    assert ec2_cleanup.should_terminate(
        SimpleNamespace(id="i-off", tags=tag, launch_time=old), None, 7 * 3600) is False


def test_leaked_molecule_instance_reaped_end_to_end(ec2_cleanup, monkeypatch):
    """A *_package_testing instance past MOLECULE_MAX_AGE_HOURS is terminated
    even though its billing tag is a valid category."""
    monkeypatch.setenv("DRY_RUN", "false")
    monkeypatch.setenv("REGIONS", REGION)
    with mock_aws():
        iid = _mk_instance(REGION, tags={"iit-billing-tag": "ps_80_package_testing"})
        with freeze_time(FUTURE):  # far past any age bound
            result = ec2_cleanup.lambda_handler({}, None)
        assert iid in [t["InstanceId"] for t in result["terminated"]]
        assert _state(REGION, iid) == "terminated"


def test_fresh_molecule_instance_spared(ec2_cleanup, monkeypatch):
    monkeypatch.setenv("DRY_RUN", "false")
    monkeypatch.setenv("REGIONS", REGION)
    with mock_aws():
        with freeze_time(FUTURE):  # created AND scanned at the same instant: age 0
            iid = _mk_instance(REGION, tags={"iit-billing-tag": "pxc_84_package_testing"})
            result = ec2_cleanup.lambda_handler({}, None)
        assert iid not in [t["InstanceId"] for t in result["terminated"]]
        assert _state(REGION, iid) == "running"


def test_invalid_molecule_pattern_degrades_to_exemption(ec2_cleanup, monkeypatch):
    """An invalid MOLECULE_BILLING_PATTERN disables only the age-bound: the
    molecule tag falls back to the permanent category exemption, and the rest
    of the reaper keeps working."""
    monkeypatch.setenv("DRY_RUN", "false")
    monkeypatch.setenv("REGIONS", REGION)
    monkeypatch.setenv("MOLECULE_BILLING_PATTERN", "(")  # invalid regex
    with mock_aws():
        molecule = _mk_instance(REGION, tags={"iit-billing-tag": "ps_80_package_testing"})
        untagged = _mk_instance(REGION)
        with freeze_time(FUTURE):
            result = ec2_cleanup.lambda_handler({}, None)
        assert _state(REGION, molecule) == "running"   # age-bound off -> exempt
        assert _state(REGION, untagged) == "terminated"  # reaper itself unaffected
        assert untagged in [t["InstanceId"] for t in result["terminated"]]


# ---- multi-region + epoch end-to-end ----

def test_two_region_aggregation(ec2_cleanup, monkeypatch):
    monkeypatch.setenv("DRY_RUN", "false")
    monkeypatch.setenv("REGIONS", "us-east-1,eu-central-1")
    with mock_aws():
        a = _mk_instance("us-east-1")
        b = _mk_instance("eu-central-1")
        with freeze_time(FUTURE):
            result = ec2_cleanup.lambda_handler({}, None)
        ids = [t["InstanceId"] for t in result["terminated"]]
        assert a in ids and b in ids
        assert _state("us-east-1", a) == "terminated"
        assert _state("eu-central-1", b) == "terminated"


def test_expired_epoch_billing_tag_terminated(ec2_cleanup, monkeypatch):
    monkeypatch.setenv("DRY_RUN", "false")
    monkeypatch.setenv("REGIONS", REGION)
    with mock_aws():
        iid = _mk_instance(REGION, tags={"iit-billing-tag": PAST_EPOCH})  # numeric, expired
        with freeze_time(FUTURE):
            result = ec2_cleanup.lambda_handler({}, None)
        assert iid in [t["InstanceId"] for t in result["terminated"]]
        assert _state(REGION, iid) == "terminated"


# ---- CFN stack protection + deletion ----

def test_eks_stack_billing_tag_spares_instance(ec2_cleanup):
    with mock_aws():
        _mk_eksctl_stack(REGION, "foo", tags={"iit-billing-tag": "jenkins"})
        fake = SimpleNamespace(id="i-eks", tags=[{"Key": "kubernetes.io/cluster/foo", "Value": "owned"}])
        clusters_to_delete: dict[str, set[str]] = {}
        # Skip pattern deliberately does NOT match "foo"; only the stack's valid
        # billing tag should spare it.
        assert ec2_cleanup.is_eks_managed(fake, REGION, "nomatch-.*", clusters_to_delete) is True
        assert clusters_to_delete == {}


def test_delete_eks_stack_deletes_untagged(ec2_cleanup):
    with mock_aws():
        _mk_eksctl_stack(REGION, "orphan")  # no billing tag
        assert ec2_cleanup.delete_eks_stack("orphan", REGION, dry_run=False) is True


def test_delete_eks_stack_dry_run_keeps_stack(ec2_cleanup):
    with mock_aws():
        _mk_eksctl_stack(REGION, "orphan")
        assert ec2_cleanup.delete_eks_stack("orphan", REGION, dry_run=True) is True
        cfn = boto3.client("cloudformation", region_name=REGION)
        status = cfn.describe_stacks(StackName="eksctl-orphan-cluster")["Stacks"][0]["StackStatus"]
        assert status != "DELETE_COMPLETE"  # dry-run did not delete


def test_delete_eks_stack_toctou_aborts_if_tagged(ec2_cleanup):
    with mock_aws():
        # Cluster was marked for deletion, but its stack gained a valid billing
        # tag before delete_eks_stack ran -- the TOCTOU guard must abort.
        _mk_eksctl_stack(REGION, "tagged", tags={"iit-billing-tag": "jenkins"})
        assert ec2_cleanup.delete_eks_stack("tagged", REGION, dry_run=False) is False
        cfn = boto3.client("cloudformation", region_name=REGION)
        assert cfn.describe_stacks(StackName="eksctl-tagged-cluster")["Stacks"]  # still present


def test_eks_orphan_full_flow_through_handler(ec2_cleanup, monkeypatch):
    """The complete EKS orphan use case in ONE lambda_handler call: scan finds
    the EKS-tagged instance, the cluster gets marked, its eksctl stack is
    deleted, the result reports it, and the instance itself is skipped (the
    stack teardown owns it), all wired through the real handler path."""
    monkeypatch.setenv("DRY_RUN", "false")
    monkeypatch.setenv("REGIONS", REGION)
    monkeypatch.setenv("EKS_SKIP_PATTERN", "pe-.*")  # 'orphan' does not match
    with mock_aws():
        _mk_eksctl_stack(REGION, "orphan")  # exists, no billing tag
        iid = _mk_instance(REGION, tags={"kubernetes.io/cluster/orphan": "owned"})
        with freeze_time(FUTURE):
            result = ec2_cleanup.lambda_handler({}, None)
        assert f"orphan ({REGION})" in result["deleted_clusters"]
        assert iid not in [t["InstanceId"] for t in result["terminated"]]
        assert _state(REGION, iid) == "running"  # instance left to the stack teardown
        # The stack is deleting or already gone (a deleted stack is no longer
        # describable by name, in moto as in real AWS).
        from botocore.exceptions import ClientError
        cfn = boto3.client("cloudformation", region_name=REGION)
        try:
            status = cfn.describe_stacks(StackName="eksctl-orphan-cluster")["Stacks"][0]["StackStatus"]
            assert status.startswith("DELETE")
        except ClientError as e:
            assert "does not exist" in str(e)


def test_default_region_discovery_path(ec2_cleanup, monkeypatch):
    """With REGIONS unset (the production default), the handler discovers
    regions via describe_regions() and still finds the instance."""
    monkeypatch.setenv("DRY_RUN", "true")  # keep the full-region sweep read-only
    with mock_aws():
        iid = _mk_instance(REGION)
        with freeze_time(FUTURE):
            result = ec2_cleanup.lambda_handler({}, None)
        assert iid in [t["InstanceId"] for t in result["terminated"]]  # would-terminate


# ---- fail-closed CFN probe + DELETE_FAILED remediation (stubbed clients) ----

def _client_error(code, message=""):
    from botocore.exceptions import ClientError
    return ClientError({"Error": {"Code": code, "Message": message}}, "DescribeStacks")


def test_cfn_probe_fails_closed_on_error(ec2_cleanup, monkeypatch):
    """A transient CFN error (throttle/AccessDenied) must PROTECT the cluster,
    never expose its stack to deletion. Only confirmed-missing fails open."""
    class ThrottlingCfn:
        def describe_stacks(self, StackName):
            raise _client_error("Throttling", "Rate exceeded")

    monkeypatch.setattr(
        ec2_cleanup.boto3, "client",
        lambda service, region_name=None: ThrottlingCfn(),
    )
    assert ec2_cleanup.eks_stack_has_valid_billing("anything", REGION) is True


def test_cfn_probe_missing_stack_is_unprotected(ec2_cleanup, monkeypatch):
    class MissingCfn:
        def describe_stacks(self, StackName):
            raise _client_error("ValidationError", f"Stack {StackName} does not exist")

    monkeypatch.setattr(
        ec2_cleanup.boto3, "client",
        lambda service, region_name=None: MissingCfn(),
    )
    assert ec2_cleanup.eks_stack_has_valid_billing("anything", REGION) is False


def test_cleanup_failed_stack_remediates_resources(ec2_cleanup, monkeypatch):
    """DELETE_FAILED stack events drive exactly three remediations: revoke the
    SG's leftover ingress, disassociate the route-table association, delete the
    route. Stubbed clients record the calls (moto cannot produce DELETE_FAILED)."""
    events = {"StackEvents": [
        {"ResourceStatus": "DELETE_FAILED", "LogicalResourceId": "Ingress",
         "ResourceType": "AWS::EC2::SecurityGroupIngress", "PhysicalResourceId": "sg-0abc|tcp|443"},
        {"ResourceStatus": "DELETE_FAILED", "LogicalResourceId": "RtAssoc",
         "ResourceType": "AWS::EC2::SubnetRouteTableAssociation", "PhysicalResourceId": "rtbassoc-0def"},
        {"ResourceStatus": "DELETE_FAILED", "LogicalResourceId": "Route",
         "ResourceType": "AWS::EC2::Route", "PhysicalResourceId": "rtb-0123_10.0.0.0/16"},
        {"ResourceStatus": "DELETE_COMPLETE", "LogicalResourceId": "Other",
         "ResourceType": "AWS::SNS::Topic", "PhysicalResourceId": "t"},
    ]}
    calls = []

    class FakeCfn:
        def describe_stack_events(self, StackName):
            return events

    class FakeEc2:
        def describe_security_groups(self, GroupIds):
            return {"SecurityGroups": [{"IpPermissions": [{"IpProtocol": "tcp"}]}]}

        def revoke_security_group_ingress(self, **kw):
            calls.append(("revoke", kw["GroupId"]))

        def disassociate_route_table(self, AssociationId):
            calls.append(("disassociate", AssociationId))

        def delete_route(self, RouteTableId, DestinationCidrBlock):
            calls.append(("delete_route", RouteTableId, DestinationCidrBlock))

    monkeypatch.setattr(
        ec2_cleanup.boto3, "client",
        lambda service, region_name=None: FakeCfn() if service == "cloudformation" else FakeEc2(),
    )
    assert ec2_cleanup.cleanup_failed_stack("eksctl-x-cluster", REGION) is True
    assert ("revoke", "sg-0abc") in calls
    assert ("disassociate", "rtbassoc-0def") in calls
    assert ("delete_route", "rtb-0123", "10.0.0.0/16") in calls
    assert len(calls) == 3  # the DELETE_COMPLETE event triggered nothing


# ---- persistent spot requests (otherwise a relaunch loop with the reaper) ----

class _FakeSpotEc2:
    """Stub EC2 client for the spot-request calls. moto launches an instance for
    a spot request but does not link the instance back to the request, so the
    request side is stubbed and records what was cancelled."""

    def __init__(self, request_type, state="active", fail=None):
        self.request_type = request_type
        self.state = state
        self.fail = fail
        self.cancelled = []

    def describe_spot_instance_requests(self, SpotInstanceRequestIds):
        if self.fail == "describe":
            raise _client_error("UnauthorizedOperation", "denied")
        return {"SpotInstanceRequests": [
            {"SpotInstanceRequestId": SpotInstanceRequestIds[0], "Type": self.request_type, "State": self.state},
        ]}

    def cancel_spot_instance_requests(self, SpotInstanceRequestIds):
        if self.fail == "cancel":
            raise _client_error("UnauthorizedOperation", "denied")
        self.cancelled.extend(SpotInstanceRequestIds)
        return {"CancelledSpotInstanceRequests": [
            {"SpotInstanceRequestId": s, "State": "cancelled"} for s in SpotInstanceRequestIds
        ]}


def _spot_instance(request_id):
    return SimpleNamespace(id="i-spot", spot_instance_request_id=request_id)


def test_boto3_instance_exposes_spot_request_attribute(ec2_cleanup):
    """Pins the attribute name the lookup relies on to the real SDK resource
    model: a renamed or misspelled attribute would otherwise send every spot
    instance down the on-demand path and keep the relaunch loop alive."""
    with mock_aws():
        inst = boto3.resource("ec2", region_name=REGION).Instance(_mk_instance(REGION))
        assert hasattr(inst, "spot_instance_request_id")
        assert ec2_cleanup.spot_request_id(inst) is None  # on-demand
    assert ec2_cleanup.spot_request_id(SimpleNamespace(spot_instance_request_id="sir-9")) == "sir-9"


def test_persistent_spot_request_cancelled(ec2_cleanup):
    fake = _FakeSpotEc2("persistent")
    assert ec2_cleanup.cancel_persistent_spot_request(_spot_instance("sir-1"), REGION, False, fake) == ("sir-1", None)
    assert fake.cancelled == ["sir-1"]


def test_one_time_spot_request_left_alone(ec2_cleanup):
    """A one-time request closes with its instance; cancelling it is noise."""
    fake = _FakeSpotEc2("one-time")
    assert ec2_cleanup.cancel_persistent_spot_request(_spot_instance("sir-2"), REGION, False, fake) == (None, None)
    assert fake.cancelled == []


def test_already_cancelled_request_not_cancelled_again(ec2_cleanup):
    """The PMM robot or a previous cycle may have cancelled it first."""
    fake = _FakeSpotEc2("persistent", state="cancelled")
    assert ec2_cleanup.cancel_persistent_spot_request(_spot_instance("sir-5"), REGION, False, fake) == (None, None)
    assert fake.cancelled == []


def test_on_demand_instance_makes_no_spot_call(ec2_cleanup):
    class NoCalls:
        def __getattr__(self, name):
            pytest.fail(f"no EC2 call expected, got {name}")

    assert ec2_cleanup.cancel_persistent_spot_request(_spot_instance(None), REGION, False, NoCalls()) == (None, None)


def test_persistent_request_dry_run_reports_without_cancelling(ec2_cleanup):
    fake = _FakeSpotEc2("persistent")
    assert ec2_cleanup.cancel_persistent_spot_request(_spot_instance("sir-3"), REGION, True, fake) == ("sir-3", None)
    assert fake.cancelled == []


def test_spot_request_error_is_reported_not_raised(ec2_cleanup):
    """A denied describe or cancel, or a failing attribute lookup, comes back
    as an error string so the caller still terminates and the result shows the
    cancel did not happen."""
    for fail in ("describe", "cancel"):
        fake = _FakeSpotEc2("persistent", fail=fail)
        cancelled, error = ec2_cleanup.cancel_persistent_spot_request(_spot_instance("sir-4"), REGION, False, fake)
        assert cancelled is None and "UnauthorizedOperation" in error
        assert fake.cancelled == []

    class Exploding:
        id = "i-boom"

        @property
        def spot_instance_request_id(self):
            raise RuntimeError("lazy load failed")

    cancelled, error = ec2_cleanup.cancel_persistent_spot_request(Exploding(), REGION, False, _FakeSpotEc2("persistent"))
    assert cancelled is None and "lazy load failed" in error


def _patch_spot_client(ec2_cleanup, monkeypatch, fake):
    """Route the handler's per-region EC2 client to the fake and leave every
    other client, and the moto EC2 resource, untouched."""
    real_client = ec2_cleanup.boto3.client
    monkeypatch.setattr(
        ec2_cleanup.boto3, "client",
        lambda service, region_name=None: fake if service == "ec2" else real_client(service, region_name=region_name),
    )


def test_untagged_spot_instance_cancels_request_then_terminates(ec2_cleanup, monkeypatch):
    """Through the real handler path: the request behind an untagged spot
    instance is cancelled while the instance is still running, then the
    instance is terminated, and the result names the cancelled request. The
    request id comes from a patched lookup because moto does not link the
    instance to its request."""
    monkeypatch.setenv("DRY_RUN", "false")
    monkeypatch.setenv("REGIONS", REGION)
    with mock_aws():
        iid = _mk_instance(REGION)  # untagged, launched by a persistent request in this story
        state_at_cancel = []

        class OrderedFake(_FakeSpotEc2):
            def cancel_spot_instance_requests(self, SpotInstanceRequestIds):
                state_at_cancel.append(_state(REGION, iid))
                return super().cancel_spot_instance_requests(SpotInstanceRequestIds)

        fake = OrderedFake("persistent")
        _patch_spot_client(ec2_cleanup, monkeypatch, fake)
        monkeypatch.setattr(ec2_cleanup, "spot_request_id", lambda instance: "sir-e2e")
        with freeze_time(FUTURE):
            result = ec2_cleanup.lambda_handler({}, None)
        row = next(t for t in result["terminated"] if t["InstanceId"] == iid)
        assert row["CancelledSpotRequest"] == "sir-e2e"
        assert row["SpotRequestError"] is None
        assert fake.cancelled == ["sir-e2e"]
        assert state_at_cancel == ["running"]  # cancelled before the terminate call
        assert _state(REGION, iid) == "terminated"
        assert result["spot_request_errors"] == 0


def test_failed_cancel_still_terminates_and_is_counted(ec2_cleanup, monkeypatch):
    """A denied cancel must not leave the untagged instance running, and must
    be visible: the row carries the error and the result counts it."""
    monkeypatch.setenv("DRY_RUN", "false")
    monkeypatch.setenv("REGIONS", REGION)
    with mock_aws():
        iid = _mk_instance(REGION)
        fake = _FakeSpotEc2("persistent", fail="cancel")
        _patch_spot_client(ec2_cleanup, monkeypatch, fake)
        monkeypatch.setattr(ec2_cleanup, "spot_request_id", lambda instance: "sir-denied")
        with freeze_time(FUTURE):
            result = ec2_cleanup.lambda_handler({}, None)
        row = next(t for t in result["terminated"] if t["InstanceId"] == iid)
        assert row["CancelledSpotRequest"] is None
        assert "UnauthorizedOperation" in row["SpotRequestError"]
        assert result["spot_request_errors"] == 1
        assert _state(REGION, iid) == "terminated"


def test_purged_spot_request_is_nothing_to_cancel(ec2_cleanup):
    """A request record that no longer exists behind a long-lived instance is not
    an error: there is nothing left to relaunch from."""
    class Purged(_FakeSpotEc2):
        def describe_spot_instance_requests(self, SpotInstanceRequestIds):
            raise _client_error("InvalidSpotInstanceRequestID.NotFound", "does not exist")

    assert ec2_cleanup.cancel_persistent_spot_request(_spot_instance("sir-gone"), REGION, False, Purged("persistent")) == (None, None)


def test_disabled_request_is_still_cancelled(ec2_cleanup):
    """The instance stopped between the scan and this lookup, so the request is
    disabled. Terminating the stopped instance would re-open a persistent
    request, so the cancel must still happen."""
    fake = _FakeSpotEc2("persistent", state="disabled")
    assert ec2_cleanup.cancel_persistent_spot_request(_spot_instance("sir-6"), REGION, False, fake) == ("sir-6", None)
    assert fake.cancelled == ["sir-6"]


def test_failed_cancel_is_counted_even_when_terminate_fails(ec2_cleanup, monkeypatch):
    """A denied cancel followed by a denied terminate must still count as a spot
    request error, and the skipped row must carry both errors."""
    old = dt.datetime.now(dt.timezone.utc) - dt.timedelta(hours=2)

    class DoomedInstance:
        id = "i-doomed"
        tags = []
        launch_time = old
        key_name = "k"
        placement = {"AvailabilityZone": f"{REGION}a"}
        spot_instance_request_id = "sir-doomed"

        def terminate(self):
            raise _client_error("UnauthorizedOperation", "terminate denied")

    class FakeResource:
        class instances:  # noqa: N801 - mirrors the boto3 collection attribute
            @staticmethod
            def filter(Filters):
                return [DoomedInstance()]

    fake_spot = _FakeSpotEc2("persistent", fail="cancel")
    monkeypatch.setattr(ec2_cleanup.boto3, "resource", lambda service, region_name=None: FakeResource())
    monkeypatch.setattr(ec2_cleanup.boto3, "client", lambda service, region_name=None: fake_spot)
    terminated, skipped, spot_errors = ec2_cleanup.process_region(REGION, False, "pe-.*", {})
    assert terminated == []
    assert spot_errors == 1
    assert len(skipped) == 1 and "terminate denied" in skipped[0]["reason"] and "spot request error" in skipped[0]["reason"]

