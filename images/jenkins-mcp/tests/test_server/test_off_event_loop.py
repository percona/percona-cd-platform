"""Tools that call the blocking Jenkins client run in FastMCP worker threads, off the event loop.

A blocking call on the loop holds /healthz, and the liveness probe then kills the pod. FastMCP runs
a plain `def` tool in a worker thread, so every tool that reaches the client is a plain function.
"""

import asyncio
import inspect
import json
import threading
import time

import pytest
from fastmcp import Client
from fastmcp.exceptions import ToolError
from loguru import logger

from mcp_jenkins.server import mcp

# Async tools that move their blocking work to a thread themselves, or never touch the client.
_ASYNC_ALLOWED = frozenset({'find_items', 'wait_for_build', 'get_readme'})

# The streaming tools and list_masters, with the module function that their body calls first.
_STREAMING = {
    'export_build_log': ('mcp_jenkins.server.build_export.jenkins', {'fullname': 'j', 'number': 1}),
    'export_build_artifact': (
        'mcp_jenkins.server.build_export.jenkins',
        {'fullname': 'j', 'number': 1, 'relative_path': 'a.txt'},
    ),
    'get_build_console_output': ('mcp_jenkins.server.build.jenkins', {'fullname': 'j', 'number': 1}),
    'get_build_console_tail': ('mcp_jenkins.server.build.jenkins', {'fullname': 'j', 'number': 1}),
    'get_build_artifact': ('mcp_jenkins.server.build.jenkins', {'fullname': 'j', 'number': 1, 'relative_path': 'a'}),
    'list_archive_artifact': (
        'mcp_jenkins.server.build_artifact_search.jenkins',
        {'fullname': 'j', 'number': 1, 'relative_path': 'a.tar.gz'},
    ),
    'extract_archive_artifact': (
        'mcp_jenkins.server.build_artifact_search.jenkins',
        {'fullname': 'j', 'number': 1, 'relative_path': 'a.tar.gz', 'member': 'm.log'},
    ),
    'list_masters': ('mcp_jenkins.server.fleet.get_fleet', {}),
}


class _Stop(Exception):  # noqa: N818
    """Ends the tool body right after the patched entry point recorded its thread."""


async def _registered() -> dict:
    return {tool.name: tool for tool in await mcp.list_tools()}


@pytest.mark.asyncio
@pytest.mark.parametrize('name', sorted(_STREAMING))
async def test_streaming_tool_is_registered_as_a_plain_function(name):
    tools = await _registered()
    assert name in tools
    assert not inspect.iscoroutinefunction(tools[name].fn)


@pytest.mark.asyncio
async def test_only_tools_that_offload_their_own_work_stay_async():
    async_tools = {name for name, tool in (await _registered()).items() if inspect.iscoroutinefunction(tool.fn)}
    assert async_tools <= _ASYNC_ALLOWED


@pytest.mark.asyncio
@pytest.mark.parametrize('name', sorted(_STREAMING))
async def test_streaming_tool_body_runs_in_a_worker_thread(mocker, name):
    target, arguments = _STREAMING[name]
    loop_thread = threading.get_ident()
    seen: list[int] = []

    def record(*_args: object, **_kwargs: object) -> None:
        seen.append(threading.get_ident())
        raise _Stop

    mocker.patch(target, side_effect=record)
    async with Client(mcp) as client:
        result = await client.call_tool(name, arguments, raise_on_error=False)

    assert result.is_error
    assert len(seen) == 1, f'{name} never reached {target}'
    assert seen[0] != loop_thread


@pytest.mark.asyncio
async def test_slow_console_read_keeps_the_event_loop_responsive(mocker):
    client_mock = mocker.Mock()
    client_mock.get_build_console_output.side_effect = lambda **_kw: time.sleep(0.5) or 'done'
    mocker.patch('mcp_jenkins.server.build.jenkins', return_value=client_mock)
    ticks = 0

    async def heartbeat() -> None:
        nonlocal ticks
        while True:  # noqa: ASYNC110 - polls loop liveness, which is the property under test
            await asyncio.sleep(0.01)
            ticks += 1

    beat = asyncio.create_task(heartbeat())
    try:
        async with Client(mcp) as client:
            result = await client.call_tool('get_build_console_output', {'fullname': 'j', 'number': 1})
    finally:
        beat.cancel()

    assert result.data == 'done'
    # On the loop the heartbeat is frozen for the whole read and stays near 0.
    assert ticks >= 10


@pytest.fixture
def capture_audit():
    captured: list[str] = []
    sink_id = logger.add(captured.append, filter=lambda r: r['extra'].get('audit', False), format='{message}')
    yield captured
    logger.remove(sink_id)


def _caller(mocker, groups):
    token = mocker.Mock(claims={'sub': 'u1', 'groups': groups}, client_id='c')
    mocker.patch('mcp_jenkins.server.audit.get_access_token', return_value=token)


@pytest.mark.asyncio
async def test_sync_read_tool_is_audited_through_fastmcp(capture_audit, mocker):
    _caller(mocker, ['some-other-group'])
    client_mock = mocker.Mock()
    client_mock.get_build_console_output.return_value = 'log'
    mocker.patch('mcp_jenkins.server.build.jenkins', return_value=client_mock)

    async with Client(mcp) as client:
        result = await client.call_tool('get_build_console_output', {'fullname': 'j', 'number': 1})

    assert result.data == 'log'
    records = [json.loads(line) for line in capture_audit]
    assert [(r['tool'], r['status']) for r in records] == [('get_build_console_output', 'ok')]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ('name', 'module', 'arguments'),
    [
        ('get_node_config', 'node', {'name': 'agent1'}),
        ('stop_build', 'build', {'fullname': 'j', 'number': 1}),
        ('set_item_config', 'item', {'fullname': 'j', 'config_xml': '<x/>'}),
    ],
)
async def test_sync_gated_tool_is_refused_to_a_reader_through_fastmcp(capture_audit, mocker, name, module, arguments):
    assert not inspect.iscoroutinefunction((await _registered())[name].fn)
    _caller(mocker, ['some-other-group'])
    entry = mocker.patch(f'mcp_jenkins.server.{module}.jenkins')

    async with Client(mcp) as client:
        with pytest.raises(ToolError, match='jenkins-mcp-writers'):
            await client.call_tool(name, arguments)

    entry.assert_not_called()
    record = json.loads(capture_audit[-1])
    assert (record['tool'], record['status'], record['deny_reason']) == (name, 'denied', 'not_writer')


@pytest.mark.asyncio
async def test_sync_gated_tool_runs_in_a_worker_thread_for_a_writer(capture_audit, mocker):
    _caller(mocker, ['jenkins-mcp-writers'])
    loop_thread = threading.get_ident()
    seen: list[int] = []
    client_mock = mocker.Mock()
    client_mock.get_node_config.side_effect = lambda **_kw: seen.append(threading.get_ident()) or '<node/>'
    mocker.patch('mcp_jenkins.server.node.jenkins', return_value=client_mock)

    async with Client(mcp) as client:
        result = await client.call_tool('get_node_config', {'name': 'agent1'})

    assert result.data == '<node/>'
    assert len(seen) == 1
    assert seen[0] != loop_thread
    record = json.loads(capture_audit[-1])
    assert (record['tool'], record['status']) == ('get_node_config', 'ok')
