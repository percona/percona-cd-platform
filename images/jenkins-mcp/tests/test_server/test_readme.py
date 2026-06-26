import re
from collections import Counter
from pathlib import Path

import pytest

from mcp_jenkins.core.fleet import Fleet, Master
from mcp_jenkins.server import mcp, readme

_READ_SET = {
    'list_masters',
    'get_readme',
    'get_all_items',
    'query_items',
    'get_item',
    'get_item_config',
    'get_item_parameters',
    'get_running_builds',
    'get_build',
    'get_build_console_output',
    'get_build_test_report',
    'get_build_parameters',
    'get_build_scripts',
    'get_all_build_artifacts',
    'get_build_artifact',
    'get_build_artifact_url',
    'get_build_history',
    'get_build_stages',
    'get_build_changeset',
    'export_build_log',
    'export_build_artifact',
    'search_build_logs',
    'get_all_nodes',
    'get_node',
    'get_node_config',
    'get_all_queue_items',
    'get_queue_item',
    'get_all_views',
    'get_view',
    'get_build_console_tail',
    'get_build_failure_summary',
    'grep_build_artifact',
    'list_archive_artifact',
    'extract_archive_artifact',
}
_OPERATE_SET = _READ_SET | {'build_item', 'replay_build', 'stop_build', 'cancel_queue_item'}
_MANAGE_SET = _OPERATE_SET | {'create_item', 'set_item_config', 'delete_item'}


def _patch_fleet(mocker, masters):
    mocker.patch('mcp_jenkins.server.readme.get_fleet', return_value=Fleet(masters=masters))


def _patch_served(mocker, names):
    mocker.patch('mcp_jenkins.server.readme._served_tool_names', new_callable=mocker.AsyncMock, return_value=names)


@pytest.mark.asyncio
async def test_get_readme_lists_fleet_and_key_sections(mocker):
    _patch_fleet(
        mocker,
        [
            Master(name='ps80', url='u', username='u', token='x'),  # noqa: S106
            Master(name='pxc', url='u', username='u', token='x'),  # noqa: S106
        ],
    )
    _patch_served(mocker, _READ_SET)

    text = await readme.get_readme()

    assert 'Masters configured: ps80, pxc' in text
    assert 'master="<name>"' in text  # documents per-call selection (generic placeholder)
    assert 'list_masters()' in text
    assert 'query_items' in text
    assert 'Read (everyone)' in text  # access tiers documented
    assert 'NEVER exposed' in text  # config/script mutation guarantee


@pytest.mark.asyncio
async def test_get_readme_handles_empty_fleet(mocker):
    _patch_fleet(mocker, [])
    _patch_served(mocker, _READ_SET)

    assert '(none configured)' in await readme.get_readme()


@pytest.mark.asyncio
async def test_get_readme_shows_operate_when_served(mocker):
    _patch_fleet(mocker, [Master(name='ps80', url='u', username='u', token='x')])  # noqa: S106
    _patch_served(mocker, _OPERATE_SET)

    text = await readme.get_readme()

    assert 'ENABLED on this instance' in text
    assert 'build_item(fullname' in text  # operate tool in the catalog
    assert 'jenkins-mcp-writers, when enabled' in text  # operate sample block present


@pytest.mark.asyncio
async def test_get_readme_hides_operate_when_read_only(mocker):
    _patch_fleet(mocker, [Master(name='ps80', url='u', username='u', token='x')])  # noqa: S106
    _patch_served(mocker, _READ_SET)

    text = await readme.get_readme()

    assert 'not enabled on this instance' in text
    assert 'build_item(' not in text  # operate catalog/sample dropped
    assert 'create_item(' not in text  # manage catalog/sample dropped
    assert '-- jenkins-mcp-writers only' not in text  # operate + manage catalog group headings dropped


@pytest.mark.asyncio
async def test_get_readme_shows_manage_when_served(mocker):
    _patch_fleet(mocker, [Master(name='ps80', url='u', username='u', token='x')])  # noqa: S106
    _patch_served(mocker, _MANAGE_SET)

    text = await readme.get_readme()

    assert 'Manage (job definitions' in text  # manage catalog group heading
    assert 'create_item(fullname' in text  # manage tool in the catalog
    assert 'delete_item(' in text
    assert 'manage (jenkins-mcp-writers, when enabled)' in text  # manage sample block present
    assert 'NEVER exposed' in text  # node/script mutation still guaranteed off


@pytest.mark.asyncio
async def test_get_readme_degrades_when_served_unknown(mocker):
    _patch_fleet(mocker, [Master(name='ps80', url='u', username='u', token='x')])  # noqa: S106
    _patch_served(mocker, None)

    text = await readme.get_readme()

    # Unknown served set -> mode-agnostic: still shows operate, with a hedged status.
    assert 'operate-mode deployments' in text
    assert 'build_item(fullname' in text


@pytest.mark.asyncio
async def test_read_set_fixture_matches_live_read_tools():
    # Gate: the hand-maintained _READ_SET fixture must equal the live read-tagged @mcp.tool set, so a
    # newly added read tool cannot silently rot the rendering fixtures above.
    tools = await mcp.list_tools()
    live_read = {t.name for t in tools if 'read' in (t.tags or set())}
    assert _READ_SET == live_read


@pytest.mark.asyncio
async def test_get_readme_documents_every_served_tool(mocker):
    # Fail-closed gate: every served (non-write) tool must appear in the get_readme guide, so the
    # curated catalog can never silently drift behind the @mcp.tool set. get_readme need not list
    # itself; write tools (node-config / script mutation) are intentionally never exposed.
    tools = await mcp.list_tools()
    required = {t.name for t in tools if 'write' not in (t.tags or set())} - {'get_readme'}

    _patch_fleet(mocker, [Master(name='ps80', url='u', username='u', token='x')])  # noqa: S106
    _patch_served(mocker, required | {'get_readme'})
    text = await readme.get_readme()

    # Whole-token match so a short tool name is not satisfied by a longer one (get_build in get_build_history).
    missing = sorted(name for name in required if not re.search(rf'\b{re.escape(name)}\b', text))
    assert not missing, f'served tools missing from the get_readme catalog: {missing}'


@pytest.mark.asyncio
async def test_readme_md_tool_count_matches_served():
    # Fail-closed gate: the hardcoded tool count in README.md must track the live @mcp.tool tiers.
    tools = await mcp.list_tools()

    def tier(tags: set | None) -> str:
        return next((x for x in ('write', 'manage', 'operate', 'read') if x in (tags or set())), 'untagged')

    counts = Counter(tier(t.tags) for t in tools)
    readme_md = Path(__file__).resolve().parents[2] / 'README.md'
    text = readme_md.read_text()
    matches = re.findall(r'(\d+)\s+tools total\s*\(\s*(\d+)\s+read,\s*(\d+)\s+operate,\s*(\d+)\s+manage\s*\)', text)
    assert len(matches) == 1, f'expected exactly one tool-count line in README.md, found {len(matches)}'
    total, read_n, operate_n, manage_n = (int(x) for x in matches[0])
    assert (read_n, operate_n, manage_n) == (counts['read'], counts['operate'], counts['manage'])
    assert total == counts['read'] + counts['operate'] + counts['manage']


def test_server_instructions_point_to_get_readme():
    # The MCP initialize() instructions field is the cross-tool orientation many clients read
    # automatically; it must be set and steer the model to the get_readme guide.
    assert mcp.instructions
    assert 'get_readme()' in mcp.instructions
