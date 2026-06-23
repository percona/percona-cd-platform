import pytest

from mcp_jenkins.core.fleet import Fleet, Master
from mcp_jenkins.server import readme

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
    'search_build_logs',
    'get_all_nodes',
    'get_node',
    'get_node_config',
    'get_all_queue_items',
    'get_queue_item',
    'get_all_views',
    'get_view',
}
_OPERATE_SET = _READ_SET | {'build_item', 'replay_build', 'stop_build', 'cancel_queue_item'}


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
    assert 'master="pxc"' in text  # documents per-call selection
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
    assert '-- jenkins-mcp-writers only' not in text  # operate catalog group heading dropped


@pytest.mark.asyncio
async def test_get_readme_degrades_when_served_unknown(mocker):
    _patch_fleet(mocker, [Master(name='ps80', url='u', username='u', token='x')])  # noqa: S106
    _patch_served(mocker, None)

    text = await readme.get_readme()

    # Unknown served set -> mode-agnostic: still shows operate, with a hedged status.
    assert 'operate-mode deployments' in text
    assert 'build_item(fullname' in text
