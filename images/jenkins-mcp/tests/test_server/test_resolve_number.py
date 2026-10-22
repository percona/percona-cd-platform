import pathlib

import pytest

from mcp_jenkins.jenkins.model.build import Build
from mcp_jenkins.jenkins.model.item import UnknownItem, serialize_item
from mcp_jenkins.server import build
from mcp_jenkins.server.build_export import _resolve_number

_MATRIX = {
    '_class': 'hudson.matrix.MatrixProject',
    'name': 'pxc-5.7-param',
    'url': 'u',
    'fullName': 'pxc-5.7-param',
    'color': 'blue',
    'lastBuild': {'_class': 'hudson.matrix.MatrixBuild', 'number': 3, 'url': 'b'},
}
_MULTIBRANCH = {
    '_class': 'org.jenkinsci.plugins.workflow.multibranch.WorkflowMultiBranchProject',
    'name': 'mb',
    'url': 'u',
    'jobs': [],
}


def test_unknown_item_coerces_last_build():
    item = serialize_item(_MATRIX)

    assert isinstance(item, UnknownItem)
    assert isinstance(item.lastBuild, Build)
    assert item.lastBuild.number == 3


def test_resolve_number_accepts_model_dict_and_none(mocker):
    client = mocker.Mock()
    client.get_item.return_value = serialize_item(_MATRIX)
    assert _resolve_number(client, 'pxc-5.7-param', None) == 3

    client.get_item.return_value = mocker.Mock(lastBuild={'number': 7})
    assert _resolve_number(client, 'j', None) == 7

    client.get_item.return_value = serialize_item(_MULTIBRANCH)
    with pytest.raises(ValueError, match='No build found for job: mb'):
        _resolve_number(client, 'mb', None)

    assert _resolve_number(client, 'j', 5) == 5


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ('tool', 'client_method'),
    [
        ('get_build', 'get_build'),
        ('get_build_scripts', 'get_build_replay'),
        ('get_build_console_output', 'get_build_console_output'),
        ('get_build_parameters', 'get_build_parameters'),
        ('get_build_stages', 'get_build_stages'),
    ],
)
async def test_number_defaulting_tools_resolve_matrix_jobs(mocker, tool, client_method):
    client = mocker.Mock()
    client.get_item.return_value = serialize_item(_MATRIX)
    mocker.patch('mcp_jenkins.server.build.jenkins', return_value=client)

    await getattr(build, tool)(mocker.Mock(), fullname='pxc-5.7-param')

    assert getattr(client, client_method).call_args.kwargs['number'] == 3


def test_no_inline_last_build_lookups_remain():
    server_dir = pathlib.Path(build.__file__).parent
    offenders = [p.name for p in server_dir.glob('*.py') if '.lastBuild.number' in p.read_text()]

    assert offenders == []
