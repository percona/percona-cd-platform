import inspect

import pytest
from requests.exceptions import HTTPError

from mcp_jenkins.server import build
from mcp_jenkins.server.build import StopTarget


@pytest.fixture
def mock_jenkins(mocker):
    mock_jenkins = mocker.Mock()
    mocker.patch('mcp_jenkins.server.build.jenkins', return_value=mock_jenkins)
    yield mock_jenkins


def test_is_sync_so_fastmcp_offloads_it():
    assert not inspect.iscoroutinefunction(build.stop_builds)


def test_stops_each_build_and_reports_failures_inline(mock_jenkins, mocker):
    mock_jenkins.stop_build.side_effect = [None, HTTPError('404 Client Error'), None]
    items = [StopTarget(fullname='a', number=1), StopTarget(fullname='b', number=2), StopTarget(fullname='a', number=3)]

    out = build.stop_builds(mocker.Mock(), items=items)

    assert out['results'] == [
        {'fullname': 'a', 'number': 1, 'status': 'stopped'},
        {'fullname': 'b', 'number': 2, 'status': 'error', 'error': 'HTTPError: 404 Client Error'},
        {'fullname': 'a', 'number': 3, 'status': 'stopped'},
    ]
    assert (out['stopped'], out['failed']) == (2, 1)


def test_refuses_duplicates_before_stopping_anything(mock_jenkins, mocker):
    with pytest.raises(ValueError, match='duplicates'):
        build.stop_builds(mocker.Mock(), items=[StopTarget(fullname='a', number=1)] * 2)
    mock_jenkins.stop_build.assert_not_called()


def test_refuses_more_than_fifty(mock_jenkins, mocker):
    items = [StopTarget(fullname='a', number=n) for n in range(51)]
    with pytest.raises(ValueError, match='at most 50'):
        build.stop_builds(mocker.Mock(), items=items)
    mock_jenkins.stop_build.assert_not_called()


def test_refuses_empty(mock_jenkins, mocker):
    with pytest.raises(ValueError, match='at least one'):
        build.stop_builds(mocker.Mock(), items=[])
