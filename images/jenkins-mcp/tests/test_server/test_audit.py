import json
from datetime import UTC, datetime

import pytest
from loguru import logger

from mcp_jenkins.server import audit


def test_summarize_args_allowlist_and_redaction():
    args = {
        'fullname': 'job1',
        'number': 5,
        'config_xml': '<secret/>',
        'script': 'println secret',
        'data': {'TOKEN': 'shh', 'BRANCH': 'main'},
    }
    summary = audit._summarize_args(args)

    assert summary['fullname'] == 'job1'
    assert summary['number'] == 5
    assert 'config_xml' not in summary  # sensitive, dropped
    assert 'script' not in summary  # sensitive, dropped
    assert summary['data_keys'] == ['BRANCH', 'TOKEN']  # KEYS only
    assert 'shh' not in json.dumps(summary)  # build-param VALUES never logged


def test_summarize_args_empty():
    assert audit._summarize_args(None) == {}


def test_hash_stable_truncated_and_none():
    assert audit._hash('a@b.com') == audit._hash('a@b.com')
    assert len(audit._hash('a@b.com')) == 16
    assert audit._hash(None) is None


def test_identity_anonymous(mocker):
    mocker.patch('mcp_jenkins.server.audit.get_access_token', return_value=None)
    assert audit._identity() == {'sub': 'anonymous'}


def test_identity_from_claims_hashes_email(mocker):
    token = mocker.Mock(
        claims={'sub': 'u1', 'email': 'a@b.com', 'preferred_username': 'alice', 'exp': 99},
        client_id='jenkins-mcp',
    )
    mocker.patch('mcp_jenkins.server.audit.get_access_token', return_value=token)

    ident = audit._identity()
    assert ident['sub'] == 'u1'
    assert ident['preferred_username'] == 'alice'
    assert ident['token_exp'] == 99
    assert ident['email_hash'] and 'a@b.com' not in str(ident)  # email hashed, never raw


@pytest.fixture
def capture_audit():
    captured: list[str] = []
    sink_id = logger.add(captured.append, filter=lambda r: r['extra'].get('audit', False), format='{message}')
    yield captured
    logger.remove(sink_id)


@pytest.mark.asyncio
async def test_middleware_emits_read_record(capture_audit, mocker):
    mocker.patch(
        'mcp_jenkins.server.audit.get_access_token', return_value=mocker.Mock(claims={'sub': 'u1'}, client_id='c')
    )
    mocker.patch('mcp_jenkins.server.audit.get_http_request', side_effect=RuntimeError)

    ctx = mocker.Mock()
    ctx.message.name = 'get_all_items'
    ctx.message.arguments = {'master': 'ps80'}
    ctx.timestamp = datetime.now(UTC)

    async def call_next(_):
        return 'result'

    result = await audit.AuditMiddleware().on_call_tool(ctx, call_next)

    assert result == 'result'
    rec = json.loads(capture_audit[-1])
    assert rec['tool'] == 'get_all_items'
    assert rec['sub'] == 'u1'
    assert rec['master'] == 'ps80'
    assert rec['is_write'] is False
    assert rec['status'] == 'ok'
    assert 'duration_ms' in rec


@pytest.mark.asyncio
async def test_middleware_error_on_read_tool(capture_audit, mocker):
    mocker.patch('mcp_jenkins.server.audit.get_access_token', return_value=None)
    mocker.patch('mcp_jenkins.server.audit.get_http_request', side_effect=RuntimeError)

    ctx = mocker.Mock()
    ctx.message.name = 'get_all_items'
    ctx.message.arguments = {}
    ctx.timestamp = datetime.now(UTC)

    async def call_next(_):
        msg = 'boom'
        raise RuntimeError(msg)

    with pytest.raises(RuntimeError):
        await audit.AuditMiddleware().on_call_tool(ctx, call_next)

    rec = json.loads(capture_audit[-1])
    assert rec['status'] == 'error'
    assert rec['error'] == 'RuntimeError'
    assert rec['sub'] == 'anonymous'


@pytest.mark.asyncio
async def test_operate_tool_denied_without_writers_group(capture_audit, mocker):
    from fastmcp.exceptions import ToolError

    mocker.patch('mcp_jenkins.server.audit.get_access_token', return_value=None)  # anonymous, no groups
    mocker.patch('mcp_jenkins.server.audit.get_http_request', side_effect=RuntimeError)

    ctx = mocker.Mock()
    ctx.message.name = 'build_item'
    ctx.message.arguments = {'fullname': 'j', 'data': {'P': 'v'}}
    ctx.timestamp = datetime.now(UTC)

    called = {'ran': False}

    async def call_next(_):
        called['ran'] = True
        return 'ran'

    with pytest.raises(ToolError):
        await audit.AuditMiddleware().on_call_tool(ctx, call_next)

    assert called['ran'] is False  # denied before the tool executed
    rec = json.loads(capture_audit[-1])
    assert rec['tool'] == 'build_item'
    assert rec['is_write'] is True
    assert rec['status'] == 'denied'


@pytest.mark.asyncio
async def test_operate_tool_allowed_with_writers_group(capture_audit, mocker):
    token = mocker.Mock(claims={'sub': 'u1', 'groups': ['jenkins-mcp-writers']}, client_id='c')
    mocker.patch('mcp_jenkins.server.audit.get_access_token', return_value=token)
    mocker.patch('mcp_jenkins.server.audit.get_http_request', side_effect=RuntimeError)

    ctx = mocker.Mock()
    ctx.message.name = 'build_item'
    ctx.message.arguments = {'fullname': 'j', 'data': {'P': 'secret'}}
    ctx.timestamp = datetime.now(UTC)

    async def call_next(_):
        return 'queued'

    result = await audit.AuditMiddleware().on_call_tool(ctx, call_next)

    assert result == 'queued'
    rec = json.loads(capture_audit[-1])
    assert rec['status'] == 'ok'
    assert rec['is_write'] is True
    assert rec['args'] == {'fullname': 'j', 'data_keys': ['P']}  # param value 'secret' never logged


@pytest.mark.asyncio
async def test_manage_tool_denied_without_writers_group(capture_audit, mocker):
    from fastmcp.exceptions import ToolError

    mocker.patch('mcp_jenkins.server.audit.get_access_token', return_value=None)  # anonymous, no groups
    mocker.patch('mcp_jenkins.server.audit.get_http_request', side_effect=RuntimeError)

    ctx = mocker.Mock()
    ctx.message.name = 'create_item'
    ctx.message.arguments = {'fullname': 'j', 'config_xml': '<project/>'}
    ctx.timestamp = datetime.now(UTC)

    called = {'ran': False}

    async def call_next(_):
        called['ran'] = True
        return 'ran'

    with pytest.raises(ToolError):
        await audit.AuditMiddleware().on_call_tool(ctx, call_next)

    assert called['ran'] is False  # denied before the tool executed
    rec = json.loads(capture_audit[-1])
    assert rec['tool'] == 'create_item'
    assert rec['is_write'] is True
    assert rec['status'] == 'denied'
    assert 'config_xml' not in rec['args']  # sensitive arg never logged


@pytest.mark.asyncio
async def test_manage_tool_allowed_with_writers_group(capture_audit, mocker):
    token = mocker.Mock(claims={'sub': 'u1', 'groups': ['jenkins-mcp-writers']}, client_id='c')
    mocker.patch('mcp_jenkins.server.audit.get_access_token', return_value=token)
    mocker.patch('mcp_jenkins.server.audit.get_http_request', side_effect=RuntimeError)

    ctx = mocker.Mock()
    ctx.message.name = 'delete_item'
    ctx.message.arguments = {'fullname': 'j'}
    ctx.timestamp = datetime.now(UTC)

    async def call_next(_):
        return 'deleted'

    result = await audit.AuditMiddleware().on_call_tool(ctx, call_next)

    assert result == 'deleted'
    rec = json.loads(capture_audit[-1])
    assert rec['status'] == 'ok'
    assert rec['is_write'] is True


@pytest.mark.asyncio
async def test_get_item_config_denied_without_writers_group(capture_audit, mocker):
    from fastmcp.exceptions import ToolError

    mocker.patch('mcp_jenkins.server.audit.get_access_token', return_value=None)  # anonymous, no groups
    mocker.patch('mcp_jenkins.server.audit.get_http_request', side_effect=RuntimeError)

    ctx = mocker.Mock()
    ctx.message.name = 'get_item_config'
    ctx.message.arguments = {'fullname': 'j'}
    ctx.timestamp = datetime.now(UTC)

    called = {'ran': False}

    async def call_next(_):
        called['ran'] = True
        return '<project/>'

    with pytest.raises(ToolError):
        await audit.AuditMiddleware().on_call_tool(ctx, call_next)

    assert called['ran'] is False  # config.xml read denied before the tool executed
    rec = json.loads(capture_audit[-1])
    assert rec['tool'] == 'get_item_config'
    assert rec['status'] == 'denied'
    assert rec['is_write'] is False  # a gated READ, not a write


@pytest.mark.asyncio
async def test_get_item_config_allowed_with_writers_group(capture_audit, mocker):
    token = mocker.Mock(claims={'sub': 'u1', 'groups': ['jenkins-mcp-writers']}, client_id='c')
    mocker.patch('mcp_jenkins.server.audit.get_access_token', return_value=token)
    mocker.patch('mcp_jenkins.server.audit.get_http_request', side_effect=RuntimeError)

    ctx = mocker.Mock()
    ctx.message.name = 'get_item_config'
    ctx.message.arguments = {'fullname': 'j'}
    ctx.timestamp = datetime.now(UTC)

    async def call_next(_):
        return '<project/>'

    result = await audit.AuditMiddleware().on_call_tool(ctx, call_next)

    assert result == '<project/>'
    rec = json.loads(capture_audit[-1])
    assert rec['status'] == 'ok'
    assert rec['is_write'] is False


@pytest.mark.asyncio
async def test_gated_tool_denied_when_groups_claim_is_a_string(capture_audit, mocker):
    from fastmcp.exceptions import ToolError

    # A non-list groups claim must not fail open via substring matching.
    token = mocker.Mock(claims={'sub': 'u1', 'groups': 'prefix-jenkins-mcp-writers-suffix'}, client_id='c')
    mocker.patch('mcp_jenkins.server.audit.get_access_token', return_value=token)
    mocker.patch('mcp_jenkins.server.audit.get_http_request', side_effect=RuntimeError)

    ctx = mocker.Mock()
    ctx.message.name = 'create_item'
    ctx.message.arguments = {'fullname': 'j', 'config_xml': '<project/>'}
    ctx.timestamp = datetime.now(UTC)

    called = {'ran': False}

    async def call_next(_):
        called['ran'] = True
        return 'ran'

    with pytest.raises(ToolError):
        await audit.AuditMiddleware().on_call_tool(ctx, call_next)

    assert called['ran'] is False  # string claim does not satisfy list membership
    assert json.loads(capture_audit[-1])['status'] == 'denied'


def test_config_write_tools_are_in_the_group_gated_union():
    # Invariant: config-WRITE (RCE-class) tools must be writers-gated, not just excluded by mode tags.
    assert audit._CONFIG_TOOLS <= audit._GROUP_GATED
    assert {'run_groovy_script', 'set_node_config'} <= audit._GROUP_GATED


@pytest.mark.asyncio
@pytest.mark.parametrize('tool_name', ['run_groovy_script', 'set_node_config'])
async def test_config_write_tool_denied_without_writers_group(capture_audit, mocker, tool_name):
    from fastmcp.exceptions import ToolError

    # Defense in depth: these tools are normally never registered, but IF one is (a mode-filter
    # regression), it must be gated to the writers group, not open to any authenticated SSO user.
    mocker.patch('mcp_jenkins.server.audit.get_access_token', return_value=None)  # anonymous, no groups
    mocker.patch('mcp_jenkins.server.audit.get_http_request', side_effect=RuntimeError)

    ctx = mocker.Mock()
    ctx.message.name = tool_name
    ctx.message.arguments = {'name': 'agent1', 'script': 'println secret'}
    ctx.timestamp = datetime.now(UTC)

    called = {'ran': False}

    async def call_next(_):
        called['ran'] = True
        return 'ran'

    with pytest.raises(ToolError):
        await audit.AuditMiddleware().on_call_tool(ctx, call_next)

    assert called['ran'] is False  # gated before the RCE-class tool executed
    rec = json.loads(capture_audit[-1])
    assert rec['tool'] == tool_name
    assert rec['is_write'] is True
    assert rec['status'] == 'denied'
    assert 'script' not in rec['args']  # sensitive arg never logged


@pytest.mark.asyncio
async def test_config_write_tool_allowed_with_writers_group(capture_audit, mocker):
    token = mocker.Mock(claims={'sub': 'u1', 'groups': ['jenkins-mcp-writers']}, client_id='c')
    mocker.patch('mcp_jenkins.server.audit.get_access_token', return_value=token)
    mocker.patch('mcp_jenkins.server.audit.get_http_request', side_effect=RuntimeError)

    ctx = mocker.Mock()
    ctx.message.name = 'run_groovy_script'
    ctx.message.arguments = {'script': 'println secret'}
    ctx.timestamp = datetime.now(UTC)

    async def call_next(_):
        return 'out'

    result = await audit.AuditMiddleware().on_call_tool(ctx, call_next)

    assert result == 'out'
    rec = json.loads(capture_audit[-1])
    assert rec['status'] == 'ok'
    assert rec['is_write'] is True
    assert 'script' not in rec['args']  # script body never logged


@pytest.mark.asyncio
async def test_middleware_increments_metrics(capture_audit, mocker):
    from prometheus_client import REGISTRY

    mocker.patch('mcp_jenkins.server.audit.get_access_token', return_value=None)
    mocker.patch('mcp_jenkins.server.audit.get_http_request', side_effect=RuntimeError)

    ctx = mocker.Mock()
    ctx.message.name = 'get_all_items'
    ctx.message.arguments = {}
    ctx.timestamp = datetime.now(UTC)

    async def call_next(_):
        return 'ok'

    labels = {'tool': 'get_all_items', 'status': 'ok', 'is_write': 'false', 'master': 'none'}
    before = REGISTRY.get_sample_value('mcp_tool_calls_total', labels) or 0.0
    await audit.AuditMiddleware().on_call_tool(ctx, call_next)
    after = REGISTRY.get_sample_value('mcp_tool_calls_total', labels) or 0.0
    assert after == before + 1


@pytest.mark.asyncio
async def test_middleware_metric_master_label_known_passes_through(capture_audit, mocker):
    from prometheus_client import REGISTRY

    mocker.patch('mcp_jenkins.server.audit.get_access_token', return_value=None)
    mocker.patch('mcp_jenkins.server.audit.get_http_request', side_effect=RuntimeError)
    mocker.patch('mcp_jenkins.server.audit.get_fleet', return_value=mocker.Mock(names=lambda: ['pxc', 'ps80']))

    ctx = mocker.Mock()
    ctx.message.name = 'get_build'
    ctx.message.arguments = {'master': 'pxc'}
    ctx.timestamp = datetime.now(UTC)

    async def call_next(_):
        return 'ok'

    labels = {'tool': 'get_build', 'status': 'ok', 'is_write': 'false', 'master': 'pxc'}
    before = REGISTRY.get_sample_value('mcp_tool_calls_total', labels) or 0.0
    await audit.AuditMiddleware().on_call_tool(ctx, call_next)
    after = REGISTRY.get_sample_value('mcp_tool_calls_total', labels) or 0.0
    assert after == before + 1


@pytest.mark.asyncio
async def test_middleware_metric_master_label_unknown_clamped_to_invalid(capture_audit, mocker):
    from prometheus_client import REGISTRY

    mocker.patch('mcp_jenkins.server.audit.get_access_token', return_value=None)
    mocker.patch('mcp_jenkins.server.audit.get_http_request', side_effect=RuntimeError)
    mocker.patch('mcp_jenkins.server.audit.get_fleet', return_value=mocker.Mock(names=lambda: ['pxc']))

    ctx = mocker.Mock()
    ctx.message.name = 'get_build'
    ctx.message.arguments = {'master': 'bogus-not-in-fleet'}
    ctx.timestamp = datetime.now(UTC)

    async def call_next(_):
        return 'ok'

    labels = {'tool': 'get_build', 'status': 'ok', 'is_write': 'false', 'master': 'invalid'}
    before = REGISTRY.get_sample_value('mcp_tool_calls_total', labels) or 0.0
    await audit.AuditMiddleware().on_call_tool(ctx, call_next)
    after = REGISTRY.get_sample_value('mcp_tool_calls_total', labels) or 0.0
    assert after == before + 1  # bogus master collapses to the 'invalid' label (cardinality bound)
    assert json.loads(capture_audit[-1])['master'] == 'bogus-not-in-fleet'  # raw value kept in the log
