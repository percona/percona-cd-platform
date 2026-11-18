"""Every public client method that puts a caller value into a URL path refuses an unsafe path.

The methods are found by introspection, so a method added later with a `fullname`, `name` or
`relative_path` parameter is covered here without a change to this file.
"""

import inspect
import typing

import pytest

from mcp_jenkins.jenkins import Jenkins

_COMMON_UNSAFE = ['..', '../config.xml', 'a/../b', 'a\\b', 'a#b']
# A job fullname part may hold '%', it is percent-encoded before it reaches the URL.
_UNSAFE_VALUES = {
    'fullname': [*_COMMON_UNSAFE, '.', 'a/./b', 'a//b', 'a?b'],
    'name': [*_COMMON_UNSAFE, 'a%2fb', 'a%2E%2E', 'a?b', ''],
    'relative_path': [*_COMMON_UNSAFE, 'a%2fb', 'a%2E%2E', 'a%5Cb'],
}
_PATH_PARAMS = tuple(_UNSAFE_VALUES)
_SAMPLES_BY_NAME = {'number': 1, 'config_xml': '<project/>', 'count': 1, 'depth': 0, 'lines': 1}


def _path_methods() -> list[tuple[str, str]]:
    found = []
    for method_name, method in inspect.getmembers(Jenkins, inspect.isfunction):
        if method_name.startswith('_'):
            continue
        for param in inspect.signature(method).parameters:
            if param in _PATH_PARAMS:
                found.append((method_name, param))
    return found


def _sample(param: inspect.Parameter) -> object:
    if param.name in _SAMPLES_BY_NAME:
        return _SAMPLES_BY_NAME[param.name]
    annotation = param.annotation
    if typing.get_origin(annotation) is typing.Literal:
        return typing.get_args(annotation)[0]
    if annotation is int:
        return 1
    if annotation is bool:
        return False
    if annotation is dict:
        return {}
    return 'sample'


def _call(client: Jenkins, method_name: str, target: str, value: str) -> None:
    method = getattr(client, method_name)
    kwargs = {target: value}
    for param in inspect.signature(method).parameters.values():
        if param.name in kwargs or param.default is not inspect.Parameter.empty:
            continue
        kwargs[param.name] = _sample(param)
    result = method(**kwargs)
    if hasattr(result, '__enter__'):
        with result:
            pass


@pytest.fixture
def session(mocker):
    session = mocker.Mock()
    mocker.patch('mcp_jenkins.jenkins.rest_client.requests.Session', autospec=True, return_value=session)
    return session


def test_sweep_finds_the_path_methods():
    found = {method_name for method_name, _ in _path_methods()}
    assert {'get_item', 'get_build_artifact', 'stream_build_artifact', 'get_node', 'set_node_config'} <= found
    assert len(found) >= 25


@pytest.mark.parametrize(
    ('method_name', 'target', 'value'),
    [(method_name, target, value) for method_name, target in _path_methods() for value in _UNSAFE_VALUES[target]],
)
def test_unsafe_path_never_reaches_the_session(session, method_name, target, value):
    client = Jenkins(url='https://example.com/', username='u', password='p')

    with pytest.raises(ValueError):
        _call(client, method_name, target, value)

    session.request.assert_not_called()
    session.get.assert_not_called()
