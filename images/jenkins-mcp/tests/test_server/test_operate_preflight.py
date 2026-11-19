"""The operate preflight state, enforced through the real middleware and tool path."""

import json

import pytest
from fastmcp import Client
from fastmcp.exceptions import ToolError
from loguru import logger

from mcp_jenkins.core import fleet as core_fleet
from mcp_jenkins.server import fleet, mcp


@pytest.fixture
def capture_audit():
    captured: list[str] = []
    sink_id = logger.add(captured.append, filter=lambda r: r['extra'].get('audit', False), format='{message}')
    yield captured
    logger.remove(sink_id)


@pytest.fixture
def ps80_down_pmm_verified(mocker):
    """Operate mode after a preflight that could not reach ps80 and verified pmm."""
    state = core_fleet._OperateState()
    state.ran = True
    state.state = {'ps80': core_fleet.NOT_VERIFIED, 'pmm': core_fleet.VERIFIED}
    state.last_probe = {'ps80': 1000.0, 'pmm': 1000.0}
    mocker.patch.object(core_fleet, '_operate', state)
    mocker.patch.object(core_fleet, 'time', mocker.Mock(monotonic=lambda: 1001.0))
    writer = mocker.Mock(claims={'sub': 'w', 'groups': ['jenkins-mcp-writers']}, client_id='c')
    mocker.patch('mcp_jenkins.server.audit.get_access_token', return_value=writer)
    mocker.patch('mcp_jenkins.server.audit.get_http_request', side_effect=RuntimeError)
    mocker.patch('mcp_jenkins.core.scope.get_access_token', return_value=None)
    client = mocker.Mock()
    client.get_items.return_value = []
    client.build_item.return_value = 7
    mocker.patch('mcp_jenkins.core.lifespan.client_for', return_value=client)
    return client


@pytest.mark.asyncio
async def test_write_calls_refused_for_a_not_verified_master(ps80_down_pmm_verified, capture_audit):
    client = ps80_down_pmm_verified
    async with Client(mcp) as mcp_client:
        # A read first, so the session cache holds a ps80 client that the write calls must not reuse.
        await mcp_client.call_tool('get_all_items', {'master': 'ps80'})
        assert json.loads(capture_audit[-1])['status'] == 'ok'

        with pytest.raises(ToolError, match="master 'ps80'.*could not verify"):
            await mcp_client.call_tool('build_item', {'fullname': 'j', 'build_type': 'build', 'master': 'ps80'})
        operate = json.loads(capture_audit[-1])
        with pytest.raises(ToolError, match="master 'ps80'.*could not verify"):
            await mcp_client.call_tool('delete_item', {'fullname': 'j', 'master': 'ps80'})
        manage = json.loads(capture_audit[-1])

        await mcp_client.call_tool('build_item', {'fullname': 'j', 'build_type': 'build', 'master': 'pmm'})
        assert json.loads(capture_audit[-1])['status'] == 'ok'

    for record in (operate, manage):
        assert record['status'] == 'denied'
        assert record['deny_reason'] == 'operate_not_verified'
    client.delete_item.assert_not_called()
    client.build_item.assert_called_once_with(fullname='j', build_type='build', data=None)


@pytest.mark.asyncio
async def test_read_call_after_a_refused_write_still_works(ps80_down_pmm_verified):
    async with Client(mcp) as mcp_client:
        with pytest.raises(ToolError):
            await mcp_client.call_tool('stop_build', {'fullname': 'j', 'number': 1, 'master': 'ps80'})
        result = await mcp_client.call_tool('get_all_items', {'master': 'ps80'})
    assert result.data is not None


def test_list_masters_shows_the_preflight_state(mocker, ps80_down_pmm_verified):
    cfg = core_fleet.Fleet(
        masters=[
            core_fleet.Master(name='ps80', url='https://ps80.cd', username='svc', token='x'),  # noqa: S106
            core_fleet.Master(name='pmm', url='https://pmm.cd', username='svc', token='x'),  # noqa: S106
        ]
    )
    mocker.patch('mcp_jenkins.server.fleet.get_fleet', return_value=cfg)
    mocker.patch('mcp_jenkins.server.fleet.client_for', return_value=mocker.Mock(get_version=lambda: '2.568.3'))

    assert {m['name']: m['operate'] for m in fleet.list_masters()} == {'ps80': 'not verified', 'pmm': 'verified'}
