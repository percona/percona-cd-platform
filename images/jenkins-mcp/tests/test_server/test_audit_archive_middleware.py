"""The audit middleware stores the record of each write call in S3, and only those."""

import json
import re
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime

import pytest
from botocore.exceptions import ClientError, ConnectTimeoutError
from fastmcp.exceptions import ToolError
from loguru import logger
from prometheus_client import REGISTRY

from mcp_jenkins.core import audit_archive
from mcp_jenkins.server import audit, mcp

_KEY = re.compile(r'^audit/write/2026/11/19/20261119T080910\.000000Z-[0-9a-f]{32}\.json$')
_TS = datetime(2026, 11, 19, 8, 9, 10, tzinfo=UTC)


def _failures(reason: str) -> float:
    return REGISTRY.get_sample_value('mcp_audit_archive_failures_total', {'reason': reason}) or 0.0


@pytest.fixture
def capture_audit():
    captured: list[str] = []
    sink_id = logger.add(captured.append, filter=lambda r: r['extra'].get('audit', False), format='{message}')
    yield captured
    logger.remove(sink_id)


@pytest.fixture
def s3(monkeypatch, mocker):
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


def _caller(mocker, groups):
    token = mocker.Mock(claims={'sub': 'u1', 'preferred_username': 'alice', 'groups': groups}, client_id='c')
    mocker.patch('mcp_jenkins.server.audit.get_access_token', return_value=token)
    mocker.patch('mcp_jenkins.server.audit.get_http_request', side_effect=RuntimeError)


def _ctx(mocker, tool, arguments):
    ctx = mocker.Mock()
    ctx.fastmcp_context.fastmcp = mcp
    ctx.message.name = tool
    ctx.message.arguments = arguments
    ctx.timestamp = _TS
    return ctx


def _stored_record(s3) -> tuple[str, dict]:
    s3.put_object.assert_called_once()
    kwargs = s3.put_object.call_args.kwargs
    return kwargs['Key'], json.loads(kwargs['Body'])


@pytest.mark.asyncio
async def test_successful_write_call_stores_its_record(capture_audit, s3, mocker):
    _caller(mocker, ['jenkins-mcp-writers'])
    ctx = _ctx(mocker, 'build_item', {'fullname': 'j', 'master': 'ps80', 'data': {'P': 'v'}})

    result = await audit.AuditMiddleware().on_call_tool(ctx, mocker.AsyncMock(return_value='queued'))
    s3.drain()

    assert result == 'queued'
    key, body = _stored_record(s3)
    assert _KEY.match(key)
    assert 'alice' not in key
    # The stored object is the Loki line itself.
    assert body == json.loads(capture_audit[-1])
    assert key.endswith(f'-{body["request_id"]}.json')
    assert (body['tool'], body['status'], body['is_write'], body['master']) == ('build_item', 'ok', True, 'ps80')
    assert body['preferred_username'] == 'alice'
    assert body['args'] == {'fullname': 'j', 'master': 'ps80', 'data_keys': ['P']}


@pytest.mark.asyncio
async def test_denied_write_call_stores_its_record(capture_audit, s3, mocker):
    _caller(mocker, ['some-other-group'])
    ctx = _ctx(mocker, 'stop_build', {'fullname': 'j', 'number': 7})
    call_next = mocker.AsyncMock(return_value='stopped')

    with pytest.raises(ToolError, match='jenkins-mcp-writers'):
        await audit.AuditMiddleware().on_call_tool(ctx, call_next)
    s3.drain()

    call_next.assert_not_called()
    _, body = _stored_record(s3)
    assert (body['tool'], body['status'], body['deny_reason']) == ('stop_build', 'denied', 'not_writer')


@pytest.mark.asyncio
async def test_failed_write_call_stores_its_record(capture_audit, s3, mocker):
    _caller(mocker, ['jenkins-mcp-writers'])
    ctx = _ctx(mocker, 'replay_build', {'fullname': 'j', 'number': 3})

    with pytest.raises(RuntimeError):
        await audit.AuditMiddleware().on_call_tool(ctx, mocker.AsyncMock(side_effect=RuntimeError('boom')))
    s3.drain()

    _, body = _stored_record(s3)
    assert (body['status'], body['error']) == ('error', 'RuntimeError')


@pytest.mark.asyncio
async def test_read_call_stores_nothing(capture_audit, s3, mocker):
    _caller(mocker, ['jenkins-mcp-writers'])
    ctx = _ctx(mocker, 'get_all_items', {'master': 'ps80'})

    assert await audit.AuditMiddleware().on_call_tool(ctx, mocker.AsyncMock(return_value='items')) == 'items'
    s3.drain()

    s3.put_object.assert_not_called()
    assert json.loads(capture_audit[-1])['tool'] == 'get_all_items'


@pytest.mark.asyncio
async def test_sink_off_stores_nothing(capture_audit, monkeypatch, mocker):
    monkeypatch.delenv('MCP_AUDIT_S3_BUCKET', raising=False)
    audit_archive.bucket.cache_clear()
    client = mocker.patch.object(audit_archive, '_client')
    submit = mocker.patch.object(audit_archive._executor, 'submit')
    _caller(mocker, ['jenkins-mcp-writers'])
    ctx = _ctx(mocker, 'build_item', {'fullname': 'j'})

    assert await audit.AuditMiddleware().on_call_tool(ctx, mocker.AsyncMock(return_value='queued')) == 'queued'

    client.assert_not_called()
    submit.assert_not_called()
    audit_archive.bucket.cache_clear()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ('error', 'reason'),
    [
        (ClientError({'Error': {'Code': 'AccessDenied'}}, 'PutObject'), 'error'),
        (ConnectTimeoutError(endpoint_url='https://s3'), 'timeout'),
    ],
)
async def test_s3_failure_keeps_the_result_and_is_counted(capture_audit, s3, mocker, error, reason):
    _caller(mocker, ['jenkins-mcp-writers'])
    s3.put_object.side_effect = error
    before = _failures(reason)
    ctx = _ctx(mocker, 'build_item', {'fullname': 'j'})

    result = await audit.AuditMiddleware().on_call_tool(ctx, mocker.AsyncMock(return_value='queued'))
    s3.drain()

    assert result == 'queued'
    assert _failures(reason) == before + 1
    assert json.loads(capture_audit[-1])['status'] == 'ok'


@pytest.mark.asyncio
async def test_slow_s3_does_not_delay_the_result(capture_audit, s3, mocker):
    _caller(mocker, ['jenkins-mcp-writers'])
    release = threading.Event()
    s3.put_object.side_effect = lambda **_: release.wait(5)
    ctx = _ctx(mocker, 'build_item', {'fullname': 'j'})

    start = time.perf_counter()
    result = await audit.AuditMiddleware().on_call_tool(ctx, mocker.AsyncMock(return_value='queued'))
    elapsed = time.perf_counter() - start
    release.set()
    s3.drain()

    assert result == 'queued'
    assert elapsed < 1.0
    s3.put_object.assert_called_once()


@pytest.mark.asyncio
async def test_archive_crash_never_reaches_the_caller(capture_audit, s3, mocker):
    _caller(mocker, ['jenkins-mcp-writers'])
    mocker.patch.object(audit_archive._executor, 'submit', side_effect=RuntimeError('pool gone'))
    before = _failures('error')
    ctx = _ctx(mocker, 'build_item', {'fullname': 'j'})

    assert await audit.AuditMiddleware().on_call_tool(ctx, mocker.AsyncMock(return_value='queued')) == 'queued'
    assert _failures('error') == before + 1


@pytest.mark.asyncio
async def test_stored_record_holds_parameter_names_never_values(capture_audit, s3, mocker):
    _caller(mocker, ['jenkins-mcp-writers'])
    secret = 'hunter2-s3cr3t-value'  # noqa: S105
    ctx = _ctx(
        mocker,
        'build_item',
        {'fullname': 'j', 'build_type': 'buildWithParameters', 'data': {'PASSWORD': secret, 'BRANCH': 'main'}},
    )

    await audit.AuditMiddleware().on_call_tool(ctx, mocker.AsyncMock(return_value='queued'))
    s3.drain()

    body = s3.put_object.call_args.kwargs['Body'].decode()
    assert secret not in body
    assert 'main' not in json.loads(body)['args'].get('data_keys', [])
    assert json.loads(body)['args']['data_keys'] == ['BRANCH', 'PASSWORD']


@pytest.mark.asyncio
async def test_config_write_stores_no_config_text(capture_audit, s3, mocker):
    _caller(mocker, ['jenkins-mcp-writers'])
    ctx = _ctx(mocker, 'set_item_config', {'fullname': 'j', 'config_xml': '<authToken>tok-123</authToken>'})

    await audit.AuditMiddleware().on_call_tool(ctx, mocker.AsyncMock(return_value=None))
    s3.drain()

    body = s3.put_object.call_args.kwargs['Body'].decode()
    assert 'tok-123' not in body
    assert 'authToken' not in body
