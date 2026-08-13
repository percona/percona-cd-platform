"""Live tier-filter invariants over the real registered tool set.

test_mcp_jenkins.py asserts main() CALLS mcp.enable() with the right tags; these tests assert
the filter actually WORKS: after each mode's enable(), list_tools() serves exactly the tiers
that mode allows. This is FastMCP behaviour, so it is the surface a FastMCP upgrade can regress
without any source change.
"""

import pytest

from mcp_jenkins.server import mcp

_TIERS = ('read', 'operate', 'manage', 'write')


def _tier_of(tool):
    tags = set(tool.tags or ())
    return [t for t in _TIERS if t in tags]


@pytest.fixture
def restore_all_tools():
    # Every tool carries exactly one tier tag (asserted below), so enabling all four tiers
    # restores the unfiltered import-time state for the tests that run after these.
    yield
    mcp.enable(tags=set(_TIERS), only=True)


@pytest.mark.asyncio
async def test_every_tool_carries_exactly_one_tier_tag():
    # An untagged tool is dead (served in no mode); a doubly-tagged tool could be served in a
    # lower tier than its most dangerous capability. Both are classification bugs.
    tools = await mcp.list_tools()

    assert tools, 'tool registry is empty'
    for tool in tools:
        assert len(_tier_of(tool)) == 1, f'{tool.name} carries tier tags {_tier_of(tool)}'


@pytest.mark.asyncio
async def test_operate_mode_never_serves_write_tools(restore_all_tools):
    all_names = {t.name for t in await mcp.list_tools()}

    mcp.enable(tags={'read', 'operate', 'manage'}, only=True)
    served = await mcp.list_tools()

    assert served, 'operate mode serves nothing'
    assert [t.name for t in served if 'write' in set(t.tags or ())] == []
    write_names = all_names - {t.name for t in served}
    assert {'run_groovy_script', 'set_node_config'} <= write_names


@pytest.mark.asyncio
async def test_read_only_mode_serves_only_read_tools(restore_all_tools):
    mcp.enable(tags={'read'}, only=True)
    served = await mcp.list_tools()

    assert served, 'read-only mode serves nothing'
    for tool in served:
        assert _tier_of(tool) == ['read'], f'{tool.name} ({_tier_of(tool)}) served in read-only mode'
