import json
from datetime import UTC, datetime

import pytest
from loguru import logger

from mcp_jenkins.core import grants
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

    # The denial message must tell the user how to get access: where to ask, the disconnect +
    # re-authenticate step, and why a plain reconnect is not enough.
    with pytest.raises(
        ToolError, match=r'ask in #opensource-jenkins.*disconnect and re-authenticate.*group-less token'
    ) as excinfo:
        await audit.AuditMiddleware().on_call_tool(ctx, call_next)

    assert 'PS-11341' not in str(excinfo.value)  # the manage-only backend-403 caveat must not leak here
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

    # Manage denials must NOT send the caller through onboarding for nothing: the backend grant
    # is pending (PS-11341), so manage tools 403 even for writers. The caveat rides the message.
    with pytest.raises(ToolError, match=r'403 even for writers today.*PS-11341'):
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


def test_summarize_args_keeps_dropped_key_names_not_values():
    summary = audit._summarize_args({'job_name': 'secret-job', 'build_number': 7, 'config_xml': '<x/>'})

    assert summary == {'dropped_keys': ['build_number', 'config_xml', 'job_name']}
    assert 'secret-job' not in json.dumps(summary)


@pytest.mark.asyncio
async def test_bad_arguments_log_error_detail_through_fastmcp(capture_audit, mocker):
    from fastmcp import Client

    from mcp_jenkins.server import mcp

    mocker.patch('mcp_jenkins.server.audit.get_access_token', return_value=None)

    async with Client(mcp) as client:
        with pytest.raises(Exception, match='fullname'):
            await client.call_tool('get_build', {'job_name': 'x', 'master': 'ps80'})

    record = json.loads(capture_audit[-1])
    assert record['status'] == 'error'
    assert record['error'] == 'ValidationError'
    assert record['error_module'] == 'fastmcp.exceptions'
    assert {'loc': 'fullname', 'type': 'missing_argument'} in record['error_detail']
    assert {'loc': 'job_name', 'type': 'unexpected_keyword_argument'} in record['error_detail']
    assert record['args'] == {'master': 'ps80', 'dropped_keys': ['job_name']}


@pytest.mark.asyncio
@pytest.mark.parametrize(('tool', 'is_write'), [('search_item_configs', False), ('stop_builds', True)])
async def test_new_gated_tools_denied_without_writers_group(capture_audit, mocker, tool, is_write):
    from fastmcp.exceptions import ToolError

    mocker.patch('mcp_jenkins.server.audit.get_access_token', return_value=None)
    mocker.patch('mcp_jenkins.server.audit.get_http_request', side_effect=RuntimeError)
    ctx = mocker.Mock()
    ctx.message.name = tool
    ctx.message.arguments = {'pattern': 'x', 'fullname_pattern': 'j'}
    ctx.timestamp = datetime.now(UTC)
    call_next = mocker.AsyncMock()

    with pytest.raises(ToolError, match='jenkins-mcp-writers'):
        await audit.AuditMiddleware().on_call_tool(ctx, call_next)

    call_next.assert_not_called()
    rec = json.loads(capture_audit[-1])
    assert (rec['status'], rec['is_write']) == ('denied', is_write)


def test_summarize_args_logs_stop_targets_not_other_item_fields():
    summary = audit._summarize_args(
        {'items': [{'fullname': 'j', 'number': 3, 'note': 'secret'}, {'fullname': 'k', 'number': 4}]}
    )

    assert summary == {'items': [{'fullname': 'j', 'number': 3}, {'fullname': 'k', 'number': 4}], 'items_count': 2}


def test_config_search_pattern_is_fingerprinted_not_logged():
    summary = audit._summarize_args({'pattern': 'ghp_secretvalue', 'fullname_pattern': '^pmm3-'}, 'search_item_configs')

    assert 'ghp_secretvalue' not in json.dumps(summary)
    assert summary['pattern_len'] == 15
    assert summary['pattern_sha'] == audit._hash('ghp_secretvalue')
    assert summary['fullname_pattern'] == '^pmm3-'
    assert audit._summarize_args({'pattern': 'ERROR'}, 'search_build_logs') == {'pattern': 'ERROR'}


def test_wrongly_typed_data_and_items_are_recorded_as_dropped():
    summary = audit._summarize_args({'data': '{"K": "V"}', 'items': 'a#1'})

    assert summary == {'dropped_keys': ['data', 'items']}


def test_error_detail_never_masks_the_real_error():
    class OddError(Exception):
        def errors(self) -> list:
            return [object()]

    assert audit._error_detail(OddError()) is None


@pytest.mark.asyncio
async def test_successful_call_logs_its_parameters_and_no_dropped_keys(capture_audit, mocker):
    mocker.patch('mcp_jenkins.server.audit.get_access_token', return_value=None)
    mocker.patch('mcp_jenkins.server.audit.get_http_request', side_effect=RuntimeError)
    ctx = mocker.Mock()
    ctx.message.name = 'get_build_console_tail'
    ctx.message.arguments = {'fullname': 'j', 'lines': 200, 'surprise': 'x'}
    ctx.timestamp = datetime.now(UTC)

    async def call_next(_):
        return 'tail'

    await audit.AuditMiddleware().on_call_tool(ctx, call_next)

    record = json.loads(capture_audit[-1])
    assert record['args'] == {'fullname': 'j', 'lines': 200}


def _service_build_ctx(mocker, fullname, data=None):
    token = mocker.Mock(
        claims={
            'sub': 's1',
            'preferred_username': 'pmm-automation',
            'groups': ['jenkins-mcp-service-accounts', 'jenkins-mcp-master-pmm'],
        },
        client_id='c',
    )
    mocker.patch('mcp_jenkins.server.audit.get_access_token', return_value=token)
    mocker.patch('mcp_jenkins.core.grants.get_access_token', return_value=token)
    mocker.patch('mcp_jenkins.core.scope.get_access_token', return_value=token)
    mocker.patch('mcp_jenkins.server.audit.get_http_request', side_effect=RuntimeError)
    mocker.patch(
        'mcp_jenkins.core.grants.get_grants',
        return_value={'pmm-automation': {'pmm': {'pmm3-nightly-orchestrator': grants.JobRule()}}},
    )
    ctx = mocker.Mock()
    ctx.message.name = 'build_item'
    ctx.message.arguments = {'fullname': fullname, 'build_type': 'build', **({'data': data} if data else {})}
    ctx.timestamp = datetime.now(UTC)
    return ctx


@pytest.mark.asyncio
async def test_service_account_build_on_granted_job_runs(capture_audit, mocker):
    ctx = _service_build_ctx(mocker, 'pmm3-nightly-orchestrator')

    async def call_next(_):
        return 'queued'

    assert await audit.AuditMiddleware().on_call_tool(ctx, call_next) == 'queued'
    rec = json.loads(capture_audit[-1])
    assert rec['status'] == 'ok'
    assert rec['is_write'] is True
    assert rec['preferred_username'] == 'pmm-automation'


@pytest.mark.asyncio
async def test_service_account_build_on_ungranted_job_denied(capture_audit, mocker):
    from fastmcp.exceptions import ToolError

    ctx = _service_build_ctx(mocker, 'pmm3-ha-eks')
    called = {'ran': False}

    async def call_next(_):
        called['ran'] = True

    with pytest.raises(ToolError, match='not granted'):
        await audit.AuditMiddleware().on_call_tool(ctx, call_next)
    assert called['ran'] is False
    rec = json.loads(capture_audit[-1])
    assert rec['status'] == 'denied'
    assert rec['deny_reason'] == 'not_granted'


@pytest.mark.parametrize('tool', ['replay_build', 'stop_build', 'stop_builds', 'cancel_queue_item', 'get_item_config'])
@pytest.mark.asyncio
async def test_service_account_grant_covers_build_item_only(capture_audit, mocker, tool):
    from fastmcp.exceptions import ToolError

    ctx = _service_build_ctx(mocker, 'pmm3-nightly-orchestrator')
    ctx.message.name = tool

    async def call_next(_):
        return 'ran'

    with pytest.raises(ToolError, match='jenkins-mcp-writers'):
        await audit.AuditMiddleware().on_call_tool(ctx, call_next)


@pytest.mark.asyncio
async def test_service_account_in_writers_group_still_needs_a_grant(capture_audit, mocker):
    from fastmcp.exceptions import ToolError

    ctx = _service_build_ctx(mocker, 'pmm3-ha-eks')
    token = audit.get_access_token()
    token.claims['groups'].append('jenkins-mcp-writers')

    async def call_next(_):
        return 'ran'

    with pytest.raises(ToolError, match='not granted'):
        await audit.AuditMiddleware().on_call_tool(ctx, call_next)
    ctx.message.name = 'stop_build'
    with pytest.raises(ToolError, match='jenkins-mcp-writers'):
        await audit.AuditMiddleware().on_call_tool(ctx, call_next)


@pytest.mark.asyncio
async def test_malformed_service_build_is_audited_and_refused(capture_audit, mocker):
    from fastmcp.exceptions import ToolError

    ctx = _service_build_ctx(mocker, ['pmm3-nightly-orchestrator'])

    async def call_next(_):
        return 'ran'

    with pytest.raises(ToolError):
        await audit.AuditMiddleware().on_call_tool(ctx, call_next)
    rec = json.loads(capture_audit[-1])
    assert rec['status'] == 'denied'
    assert rec['deny_reason'] == 'not_granted'


@pytest.mark.asyncio
async def test_granted_build_records_the_resolved_master(capture_audit, mocker):
    ctx = _service_build_ctx(mocker, 'pmm3-nightly-orchestrator')

    async def call_next(_):
        return 'queued'

    await audit.AuditMiddleware().on_call_tool(ctx, call_next)
    assert json.loads(capture_audit[-1])['master'] == 'pmm'


@pytest.mark.asyncio
@pytest.mark.parametrize('wrapped', [False, True])
async def test_middleware_turns_jenkins_404_into_not_found(capture_audit, mocker, wrapped):
    from fastmcp.exceptions import ToolError
    from requests.exceptions import HTTPError

    mocker.patch('mcp_jenkins.server.audit.get_access_token', return_value=None)
    mocker.patch('mcp_jenkins.server.audit.get_http_request', side_effect=RuntimeError)

    ctx = mocker.Mock()
    ctx.message.name = 'get_build_walk'
    ctx.message.arguments = {'master': 'ps80', 'fullname': 'no-such-job'}
    ctx.timestamp = datetime.now(UTC)
    response = mocker.Mock(status_code=404, url='https://ps80.cd.percona.com/job/no-such-job/api/json?tree=x')
    http_error = HTTPError('404 Client Error: Not Found', response=response)

    async def call_next(_):
        if wrapped:
            msg = "Error calling tool 'get_build_walk'"
            raise ToolError(msg) from http_error
        raise http_error

    with pytest.raises(ToolError, match=r'^Not found on ps80: /job/no-such-job/api/json does not exist\. '):
        await audit.AuditMiddleware().on_call_tool(ctx, call_next)

    rec = json.loads(capture_audit[-1])
    assert rec['status'] == 'error'
    assert rec['error'] == ('ToolError' if wrapped else 'HTTPError')


@pytest.mark.asyncio
async def test_middleware_keeps_other_http_errors(capture_audit, mocker):
    from requests.exceptions import HTTPError

    mocker.patch('mcp_jenkins.server.audit.get_access_token', return_value=None)
    mocker.patch('mcp_jenkins.server.audit.get_http_request', side_effect=RuntimeError)

    ctx = mocker.Mock()
    ctx.message.name = 'get_build'
    ctx.message.arguments = {'master': 'ps80'}
    ctx.timestamp = datetime.now(UTC)

    async def call_next(_):
        msg = '500 Server Error'
        raise HTTPError(msg, response=mocker.Mock(status_code=500, url='u'))

    with pytest.raises(HTTPError, match='500 Server Error'):
        await audit.AuditMiddleware().on_call_tool(ctx, call_next)
