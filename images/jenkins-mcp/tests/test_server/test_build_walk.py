import inspect

import pytest
from requests.exceptions import HTTPError

from mcp_jenkins.jenkins.model.build import PipelineStage, PipelineStages
from mcp_jenkins.server import build


@pytest.fixture
def mock_jenkins(mocker):
    mock_jenkins = mocker.Mock()
    mocker.patch('mcp_jenkins.server.build.jenkins', return_value=mock_jenkins)
    yield mock_jenkins


def _raw(number: int, *, params: list | None = None, report: dict | None = None) -> dict:
    actions = [{'_class': 'hudson.model.CauseAction'}]
    if params is not None:
        actions.append({'_class': 'hudson.model.ParametersAction', 'parameters': params})
    if report is not None:
        actions.append({'_class': 'hudson.tasks.junit.TestResultAction', **report})
    return {
        'number': number,
        'result': 'FAILURE',
        'building': False,
        'timestamp': 1,
        'duration': 2,
        'url': f'u{number}',
        'queueId': 100 + number,
        'actions': actions,
    }


def _stages(*names: str) -> PipelineStages:
    return PipelineStages(
        stages=[PipelineStage(id=str(i), name=n, status='SUCCESS', durationMillis=5) for i, n in enumerate(names)]
    )


def test_walk_is_sync_so_fastmcp_offloads_it():
    assert not inspect.iscoroutinefunction(build.get_build_walk)


def test_walk_parses_parameters_test_counts_and_stages(mock_jenkins, mocker):
    mock_jenkins.get_build_walk.return_value = [
        _raw(2, params=[{'name': 'BRANCH', 'value': '8.4'}], report={'failCount': 2, 'skipCount': 1, 'totalCount': 10}),
        _raw(1),
    ]
    mock_jenkins.get_build_stages.side_effect = [_stages('Build', 'Test'), _stages('Build')]

    result = build.get_build_walk(mocker.Mock(), fullname='job1', count=2)

    first, second = result['builds']
    assert first['queue_id'] == 102
    assert first['parameters'] == {'BRANCH': '8.4'}
    assert first['test_counts'] == {'fail': 2, 'skip': 1, 'pass': 7, 'total': 10}
    assert [s['name'] for s in first['stages']] == ['Build', 'Test']
    assert first['stages'][0] == {'name': 'Build', 'status': 'SUCCESS', 'durationMillis': 5}
    assert second['parameters'] == {}
    assert 'test_counts' not in second
    assert result['errors'] == []
    mock_jenkins.get_build_walk.assert_called_once_with(fullname='job1', count=2)


def test_walk_include_skips_stage_requests(mock_jenkins, mocker):
    mock_jenkins.get_build_walk.return_value = [_raw(1, params=[])]

    result = build.get_build_walk(mocker.Mock(), fullname='job1', include=['parameters'])

    mock_jenkins.get_build_stages.assert_not_called()
    assert 'stages' not in result['builds'][0]
    assert 'test_counts' not in result['builds'][0]


def test_walk_caps_count(mock_jenkins, mocker):
    mock_jenkins.get_build_walk.return_value = []

    result = build.get_build_walk(mocker.Mock(), fullname='job1', count=500, include=['parameters'])

    mock_jenkins.get_build_walk.assert_called_once_with(fullname='job1', count=30)
    assert result['notes'] == ['count adjusted from 500 to 30']


def test_walk_keeps_going_when_one_stage_fetch_fails(mock_jenkins, mocker):
    mock_jenkins.get_build_walk.return_value = [_raw(2), _raw(1)]
    mock_jenkins.get_build_stages.side_effect = [HTTPError('500 Server Error'), _stages('Build')]

    result = build.get_build_walk(mocker.Mock(), fullname='job1', include=['stages'])

    assert 'stages' not in result['builds'][0]
    assert result['builds'][1]['stages'][0]['name'] == 'Build'
    assert result['errors'] == [{'number': 2, 'step': 'stages', 'error': 'HTTPError: 500 Server Error'}]


def test_walk_stops_stage_requests_after_budget(mock_jenkins, mocker):
    mock_jenkins.get_build_walk.return_value = [_raw(2), _raw(1)]
    mock_jenkins.get_build_stages.return_value = _stages('Build')
    clock = iter([0.0, 1.0, 1000.0])
    mocker.patch('mcp_jenkins.server.build.time.monotonic', side_effect=lambda: next(clock))

    result = build.get_build_walk(mocker.Mock(), fullname='job1', include=['stages'])

    assert mock_jenkins.get_build_stages.call_count == 1
    assert 'stages' not in result['builds'][1]
    assert 'stages budget' in result['notes'][0]


def test_walk_empty_include_fetches_nothing_extra(mock_jenkins, mocker):
    mock_jenkins.get_build_walk.return_value = [_raw(1, params=[{'name': 'A', 'value': '1'}])]

    result = build.get_build_walk(mocker.Mock(), fullname='job1', include=[])

    mock_jenkins.get_build_stages.assert_not_called()
    assert set(result['builds'][0]) == {'number', 'result', 'building', 'timestamp', 'duration', 'url', 'queue_id'}
