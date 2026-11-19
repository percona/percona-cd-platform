import json
import re
import threading
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime, timedelta, timezone

import pytest
from botocore.exceptions import ClientError, ReadTimeoutError
from prometheus_client import REGISTRY

from mcp_jenkins.core import audit_archive

_KEY = re.compile(r'^audit/write/\d{4}/\d{2}/\d{2}/\d{8}T\d{6}\.\d{6}Z-[0-9a-f]{32}\.json$')


def _failures(reason: str) -> float:
    return REGISTRY.get_sample_value('mcp_audit_archive_failures_total', {'reason': reason}) or 0.0


def _stored() -> float:
    return REGISTRY.get_sample_value('mcp_audit_archive_stored_total') or 0.0


@pytest.fixture
def sink(monkeypatch, mocker):
    """The sink switched on, with a mocked S3 client and a private pool the test can drain."""
    monkeypatch.setenv('MCP_AUDIT_S3_BUCKET', 'audit-bucket')
    audit_archive.bucket.cache_clear()
    client = mocker.Mock()
    mocker.patch.object(audit_archive, '_client', return_value=client)
    pool = ThreadPoolExecutor(max_workers=2)
    mocker.patch.object(audit_archive, '_executor', pool)
    client.drain = lambda: pool.shutdown(wait=True)
    yield client
    pool.shutdown(wait=True)
    audit_archive.bucket.cache_clear()


@pytest.fixture
def sink_off(monkeypatch):
    monkeypatch.delenv('MCP_AUDIT_S3_BUCKET', raising=False)
    audit_archive.bucket.cache_clear()
    yield
    audit_archive.bucket.cache_clear()


def test_object_key_sorts_by_time_and_has_no_user():
    ts = datetime(2026, 11, 19, 7, 5, 3, 120000, tzinfo=UTC)
    key = audit_archive.object_key(ts, 'ab' * 16)
    assert key == 'audit/write/2026/11/19/20261119T070503.120000Z-' + 'ab' * 16 + '.json'
    later = audit_archive.object_key(ts + timedelta(microseconds=1), '00' * 16)
    assert sorted([later, key]) == [key, later]


def test_object_key_converts_to_utc_and_cleans_the_random_part():
    berlin = timezone(timedelta(hours=1))
    key = audit_archive.object_key(datetime(2026, 11, 19, 0, 30, tzinfo=berlin), '../ALICE/x')
    assert key.startswith('audit/write/2026/11/18/20261118T233000.000000Z-')
    assert _KEY.match(key)
    assert 'alice' not in key.lower()
    assert '..' not in key


def test_bucket_off_by_default(sink_off, mocker):
    client = mocker.patch.object(audit_archive, '_client')
    assert audit_archive.bucket() is None
    assert audit_archive.archive('{}', datetime.now(UTC), 'ab' * 16) is None
    client.assert_not_called()


def test_blank_bucket_is_off(monkeypatch):
    monkeypatch.setenv('MCP_AUDIT_S3_BUCKET', '  ')
    audit_archive.bucket.cache_clear()
    assert audit_archive.bucket() is None
    audit_archive.bucket.cache_clear()


def test_archive_puts_one_object(sink):
    before = _stored()
    line = json.dumps({'request_id': 'cd' * 16, 'tool': 'build_item'})
    audit_archive.archive(line, datetime(2026, 11, 19, tzinfo=UTC), 'cd' * 16)
    sink.drain()

    sink.put_object.assert_called_once()
    kwargs = sink.put_object.call_args.kwargs
    assert kwargs['Bucket'] == 'audit-bucket'
    assert _KEY.match(kwargs['Key'])
    assert kwargs['Body'] == line.encode()
    assert kwargs['ContentType'] == 'application/json'
    # A checksum header lets the put pass Object Lock, and If-None-Match refuses an overwrite.
    assert kwargs['ChecksumAlgorithm'] == 'SHA256'
    assert kwargs['IfNoneMatch'] == '*'
    assert _stored() == before + 1


def test_client_has_short_timeouts(monkeypatch, mocker):
    audit_archive._client.cache_clear()
    boto_client = mocker.patch('boto3.client')
    audit_archive._client()
    config = boto_client.call_args.kwargs['config']
    assert config.connect_timeout <= 2
    assert config.read_timeout <= 3
    assert config.retries == {'total_max_attempts': 2, 'mode': 'standard'}
    audit_archive._client.cache_clear()


def test_s3_error_is_counted_not_raised(sink):
    before = _failures('error')
    sink.put_object.side_effect = ClientError({'Error': {'Code': 'AccessDenied'}}, 'PutObject')
    audit_archive.archive('{}', datetime.now(UTC), 'ab' * 16)
    sink.drain()
    assert _failures('error') == before + 1


def test_s3_timeout_is_counted_as_timeout(sink):
    before = _failures('timeout')
    sink.put_object.side_effect = ReadTimeoutError(endpoint_url='https://s3')
    audit_archive.archive('{}', datetime.now(UTC), 'ab' * 16)
    sink.drain()
    assert _failures('timeout') == before + 1


def test_archive_never_raises_when_the_pool_refuses(sink, mocker):
    before = _failures('error')
    mocker.patch.object(audit_archive._executor, 'submit', side_effect=RuntimeError('shut down'))
    assert audit_archive.archive('{}', datetime.now(UTC), 'ab' * 16) is None
    assert _failures('error') == before + 1
    assert audit_archive._pending == 0


def test_archive_never_raises_on_a_bad_timestamp(sink):
    before = _failures('error')
    assert audit_archive.archive('{}', object(), 'ab' * 16) is None
    assert _failures('error') == before + 1


def test_backlog_drops_and_counts(sink, monkeypatch):
    release = threading.Event()
    sink.put_object.side_effect = lambda **_: release.wait(5)
    monkeypatch.setattr(audit_archive, '_MAX_PENDING', 2)
    before = _failures('backlog')
    for _ in range(3):
        audit_archive.archive('{}', datetime.now(UTC), 'ab' * 16)
    assert _failures('backlog') == before + 1
    release.set()
    sink.drain()
    assert sink.put_object.call_count == 2
    assert audit_archive._pending == 0
