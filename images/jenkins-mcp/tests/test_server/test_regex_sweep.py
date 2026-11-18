"""Every registered tool argument that takes a regex runs it on RE2 in linear time.

The sweep reads the tool registry, so a new pattern or regex argument is covered without an edit.
Each tool runs against a real client whose job names, console and configs are 5,000 '=' characters.
"""

import importlib
import inspect
import io
import time
from contextlib import contextmanager

import pytest

from mcp_jenkins.core.fleet import Fleet, Master
from mcp_jenkins.jenkins import Jenkins, rest_client
from mcp_jenkins.jenkins.model.build import Build
from mcp_jenkins.jenkins.model.item import Job
from mcp_jenkins.server import mcp

CATASTROPHIC = '(=+)+$x'
TEXT = '=' * 5000
BUDGET_SECONDS = 0.5
NARROW = 'job'
REQUIRED_VALUES = {'fullname': NARROW, 'relative_path': 'x.log'}
SERVER_MODULES = (
    'build',
    'build_artifact_search',
    'build_export',
    'fleet',
    'item',
    'node',
    'queue',
    'script',
    'search',
    'view',
)
# The sweep must at least find these, so an empty registry read cannot pass.
KNOWN = {
    ('find_items', 'fullname_pattern'),
    ('find_items', 'class_pattern'),
    ('get_build_console_output', 'pattern'),
    ('grep_build_artifact', 'pattern'),
    ('query_items', 'class_pattern'),
    ('query_items', 'color_pattern'),
    ('query_items', 'fullname_pattern'),
    ('query_items', 'label_pattern'),
    ('search_build_logs', 'job_pattern'),
    ('search_build_logs', 'pattern'),
    ('search_item_configs', 'fullname_pattern'),
    ('search_item_configs', 'pattern'),
}


def _accepts_string(schema: dict) -> bool:
    if schema.get('type') == 'string':
        return True
    return any(_accepts_string(option) for option in schema.get('anyOf', []))


async def _pattern_arguments() -> list[tuple[object, str, set[str]]]:
    found = []
    for tool in await mcp.list_tools():
        properties = (tool.parameters or {}).get('properties', {})
        names = {
            name
            for name, schema in properties.items()
            if name.endswith(('pattern', 'regex')) and _accepts_string(schema)
        }
        found.extend((tool, name, names) for name in sorted(names))
    return found


def _job(fullname: str) -> Job:
    return Job(
        fullname=fullname,
        name=fullname,
        url='u',
        class_=TEXT,
        color=TEXT,
        lastBuild=Build(number=1, url='u', building=False),
    )


@contextmanager
def _artifact_stream():
    class _Response:
        raw = io.BytesIO((TEXT + '\n').encode() * 3)
        headers: dict = {}

        def close(self) -> None:
            pass

    yield _Response()


@pytest.fixture
def equals_client(mocker):
    session = mocker.Mock()
    mocker.patch('mcp_jenkins.jenkins.rest_client.requests.Session', autospec=True, return_value=session)
    session.get.side_effect = lambda *_a, **_k: mocker.Mock(iter_lines=lambda **_kw: iter([TEXT, TEXT]))
    client = Jenkins(url='https://example.com/', username='u', password='p')
    mocker.patch.object(client, 'get_items', return_value=[_job(NARROW), _job(TEXT)])
    mocker.patch.object(client, 'get_item_config', return_value=f'<a>\n{TEXT}\n</a>')
    mocker.patch.object(
        client, 'get_build_history', side_effect=lambda **_k: [Build(number=1, url='u', building=False)]
    )
    mocker.patch.object(client, 'stream_build_artifact', side_effect=lambda **_k: _artifact_stream())
    for name in SERVER_MODULES:
        module = importlib.import_module(f'mcp_jenkins.server.{name}')
        if hasattr(module, 'jenkins'):
            mocker.patch.object(module, 'jenkins', return_value=client)
        if hasattr(module, '_selected_master'):
            mocker.patch.object(module, '_selected_master', return_value='ps80')
    fleet_cfg = Fleet(masters=[Master(name='ps80', url='https://ps80.cd', username='svc', token='x')])  # noqa: S106
    mocker.patch('mcp_jenkins.server.fleet.get_fleet', return_value=fleet_cfg)
    mocker.patch('mcp_jenkins.server.fleet.client_for', return_value=client)
    mocker.patch('mcp_jenkins.server.fleet.allowed_masters', return_value=None)
    return client


def _arguments(fn, target: str, pattern_names: set[str], mocker) -> dict:
    kwargs = {}
    for name, param in inspect.signature(fn).parameters.items():
        if name == 'ctx':
            kwargs[name] = mocker.Mock()
        elif name == target:
            kwargs[name] = CATASTROPHIC
        elif name in pattern_names and param.default is inspect.Parameter.empty:
            kwargs[name] = NARROW
        elif name in REQUIRED_VALUES:
            kwargs[name] = REQUIRED_VALUES[name]
        elif name == 'number':
            kwargs[name] = 1
        elif name == 'literal':
            kwargs[name] = False
    return kwargs


@pytest.mark.asyncio
async def test_sweep_finds_the_known_pattern_arguments():
    found = {(tool.name, name) for tool, name, _ in await _pattern_arguments()}
    assert found >= KNOWN, f'missing: {sorted(KNOWN - found)}'


@pytest.mark.asyncio
async def test_every_pattern_argument_runs_on_re2_in_linear_time(equals_client, mocker):
    compiled = mocker.spy(rest_client.re2, 'compile')
    slow = []
    for tool, target, pattern_names in await _pattern_arguments():
        compiled.reset_mock()
        kwargs = _arguments(tool.fn, target, pattern_names, mocker)
        started = time.perf_counter()
        result = tool.fn(**kwargs)
        if inspect.isawaitable(result):
            await result
        seconds = time.perf_counter() - started
        if seconds >= BUDGET_SECONDS:
            slow.append(f'{tool.name}.{target}: {seconds:.2f}s')
        reached = [call.args[0] for call in compiled.call_args_list]
        assert any(CATASTROPHIC in pattern for pattern in reached), f'{tool.name}.{target} did not reach RE2'
    assert not slow, slow
