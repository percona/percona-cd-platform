import pytest

from mcp_jenkins.jenkins.model.build import Artifact, Build, BuildReplay, ChangeSetItem, PipelineStage, PipelineStages
from mcp_jenkins.server import build


@pytest.fixture
def mock_jenkins(mocker):
    mock_jenkins = mocker.Mock()

    mocker.patch('mcp_jenkins.server.build.jenkins', return_value=mock_jenkins)

    yield mock_jenkins


@pytest.mark.asyncio
async def test_get_running_builds(mock_jenkins, mocker):
    build1 = Build(number=1, url='1', building=True, timestamp=1234567890)
    build2 = Build(number=2, url='2', building=True, timestamp=1234567891)
    mock_jenkins.get_running_builds.return_value = [build1, build2]

    assert await build.get_running_builds(mocker.Mock()) == [
        {'number': 1, 'url': '1', 'building': True, 'timestamp': 1234567890},
        {'number': 2, 'url': '2', 'building': True, 'timestamp': 1234567891},
    ]


@pytest.mark.asyncio
async def test_get_build(mock_jenkins, mocker):
    mock_jenkins.get_item.return_value.lastBuild.number = 1
    mock_jenkins.get_build.return_value = Build(number=1, url='1', building=False, timestamp=1234567890)

    assert await build.get_build(mocker.Mock(), fullname='job1') == {
        'number': 1,
        'url': '1',
        'building': False,
        'timestamp': 1234567890,
    }


@pytest.mark.asyncio
async def test_get_build_scripts(mock_jenkins, mocker):
    mock_jenkins.get_item.return_value.lastBuild.number = 1
    mock_jenkins.get_build_replay.return_value = BuildReplay(scripts=['script1', 'script2'])

    assert await build.get_build_scripts(mocker.Mock(), fullname='job1') == [
        'script1',
        'script2',
    ]


@pytest.mark.asyncio
async def test_get_build_console_output(mock_jenkins, mocker):
    mock_jenkins.get_item.return_value.lastBuild.number = 1
    mock_jenkins.get_build_console_output.return_value = 'Console output here'

    assert await build.get_build_console_output(mocker.Mock(), fullname='job1') == 'Console output here'
    mock_jenkins.get_build_console_output.assert_called_once_with(
        fullname='job1', number=1, pattern=None, offset=0, limit=None
    )


@pytest.mark.asyncio
async def test_get_build_console_output_with_number(mock_jenkins, mocker):
    mock_jenkins.get_build_console_output.return_value = 'output'

    assert await build.get_build_console_output(mocker.Mock(), fullname='job1', number=5) == 'output'
    mock_jenkins.get_item.assert_not_called()
    mock_jenkins.get_build_console_output.assert_called_once_with(
        fullname='job1', number=5, pattern=None, offset=0, limit=None
    )


@pytest.mark.asyncio
async def test_get_build_console_output_with_all_params(mock_jenkins, mocker):
    mock_jenkins.get_build_console_output.return_value = 'ERROR: boom'

    result = await build.get_build_console_output(
        mocker.Mock(), fullname='job1', number=3, pattern='ERROR', offset=1, limit=10
    )
    assert result == 'ERROR: boom'
    mock_jenkins.get_build_console_output.assert_called_once_with(
        fullname='job1', number=3, pattern='ERROR', offset=1, limit=10
    )


@pytest.mark.asyncio
async def test_get_build_console_output_no_build(mock_jenkins, mocker):
    mock_jenkins.get_item.return_value.lastBuild.number = None

    with pytest.raises(ValueError, match='No build found for job: job1'):
        await build.get_build_console_output(mocker.Mock(), fullname='job1')


@pytest.mark.asyncio
async def test_get_build_test_reports(mock_jenkins, mocker):
    mock_jenkins.get_item.return_value.lastBuild.number = 1
    mock_jenkins.get_build_test_report.return_value = {'reports': ['report1', 'report2']}

    assert await build.get_build_test_report(mocker.Mock(), fullname='job1') == {'reports': ['report1', 'report2']}


@pytest.mark.asyncio
async def test_get_build_parameters(mock_jenkins, mocker):
    mock_jenkins.get_item.return_value.lastBuild.number = 1
    mock_jenkins.get_build_parameters.return_value = {'BRANCH': 'main', 'DEBUG': True}

    assert await build.get_build_parameters(mocker.Mock(), fullname='job1') == {
        'BRANCH': 'main',
        'DEBUG': True,
    }


@pytest.mark.asyncio
async def test_stop_build(mock_jenkins, mocker):
    await build.stop_build(mocker.Mock(), fullname='job1', number=1)
    mock_jenkins.stop_build.assert_called_once_with(fullname='job1', number=1)


@pytest.mark.asyncio
async def test_get_all_build_artifacts(mock_jenkins, mocker):
    mock_jenkins.get_item.return_value.lastBuild.number = 1
    mock_jenkins.get_build_artifacts.return_value = [
        Artifact(
            fileName='index.html',
            relativePath='playwright-report/index.html',
            displayPath='playwright-report/index.html',
        ),
        Artifact(fileName='trace.zip', relativePath='trace.zip', displayPath='trace.zip'),
    ]

    assert await build.get_all_build_artifacts(mocker.Mock(), fullname='job1') == [
        {
            'fileName': 'index.html',
            'relativePath': 'playwright-report/index.html',
            'displayPath': 'playwright-report/index.html',
        },
        {
            'fileName': 'trace.zip',
            'relativePath': 'trace.zip',
            'displayPath': 'trace.zip',
            'hint': 'archive (use list_archive_artifact to see members)',
        },
    ]


@pytest.mark.asyncio
async def test_get_all_build_artifacts_with_number(mock_jenkins, mocker):
    mock_jenkins.get_build_artifacts.return_value = []

    assert await build.get_all_build_artifacts(mocker.Mock(), fullname='job1', number=5) == []
    mock_jenkins.get_item.assert_not_called()
    mock_jenkins.get_build_artifacts.assert_called_once_with(fullname='job1', number=5)


@pytest.mark.asyncio
async def test_get_all_build_artifacts_hints_classify_mtr_paths(mock_jenkins, mocker):
    # Real ps80 #1270 listing: hints must steer the caller to the right artifact without trial+error,
    # and leave an opaque stub (public_url) unlabeled rather than guess.
    mock_jenkins.get_build_artifacts.return_value = [
        Artifact(fileName='build.log.gz', relativePath='build.log.gz'),
        Artifact(fileName='make_build.log', relativePath='work/make_build.log'),
        Artifact(fileName='mtr-test_1.log', relativePath='work/mtr-test_1.log'),
        Artifact(fileName='ps80-test-mtr_logs-1.tar.gz', relativePath='work/results/ps80-test-mtr_logs-1.tar.gz'),
        Artifact(fileName='public_url', relativePath='public_url'),
    ]

    out = await build.get_all_build_artifacts(mocker.Mock(), fullname='job1', number=1270)
    hints = {a['relativePath']: a.get('hint') for a in out}
    assert hints['build.log.gz'] == 'full build console log'
    assert hints['work/make_build.log'] == 'build log'
    assert hints['work/mtr-test_1.log'].startswith('test-runner log')
    assert hints['work/results/ps80-test-mtr_logs-1.tar.gz'].startswith('per-test logs archive')
    assert hints['public_url'] is None


@pytest.mark.asyncio
async def test_get_build_artifact_text(mock_jenkins, mocker):
    mock_jenkins.get_item.return_value.lastBuild.number = 1
    mock_jenkins.get_build_artifact.return_value = b'<html>report</html>'

    result = await build.get_build_artifact(
        mocker.Mock(), fullname='job1', relative_path='playwright-report/index.html'
    )
    assert result == {'content': '<html>report</html>', 'encoding': 'utf-8'}


@pytest.mark.asyncio
async def test_get_build_artifact_binary(mock_jenkins, mocker):
    mock_jenkins.get_item.return_value.lastBuild.number = 1
    mock_jenkins.get_build_artifact.return_value = bytes(range(256))

    result = await build.get_build_artifact(mocker.Mock(), fullname='job1', relative_path='trace.zip')
    assert result['encoding'] == 'base64'
    import base64

    assert base64.b64decode(result['content']) == bytes(range(256))


@pytest.mark.asyncio
async def test_get_build_artifact_with_number(mock_jenkins, mocker):
    mock_jenkins.get_build_artifact.return_value = b'data'

    result = await build.get_build_artifact(mocker.Mock(), fullname='job1', relative_path='file.txt', number=3)
    mock_jenkins.get_item.assert_not_called()
    mock_jenkins.get_build_artifact.assert_called_once_with(fullname='job1', number=3, relative_path='file.txt')
    assert result == {'content': 'data', 'encoding': 'utf-8'}


@pytest.mark.asyncio
async def test_get_build_artifact_url(mock_jenkins, mocker):
    mock_jenkins.get_item.return_value.lastBuild.number = 1
    mock_jenkins.get_build_artifact_url.return_value = 'https://jenkins.example.com/job/job1/1/artifact/trace.zip'

    result = await build.get_build_artifact_url(mocker.Mock(), fullname='job1', relative_path='trace.zip')
    assert result == 'https://jenkins.example.com/job/job1/1/artifact/trace.zip'


@pytest.mark.asyncio
async def test_get_build_artifact_url_with_number(mock_jenkins, mocker):
    mock_jenkins.get_build_artifact_url.return_value = 'https://jenkins.example.com/job/job1/5/artifact/report.html'

    result = await build.get_build_artifact_url(mocker.Mock(), fullname='job1', relative_path='report.html', number=5)
    mock_jenkins.get_item.assert_not_called()
    mock_jenkins.get_build_artifact_url.assert_called_once_with(fullname='job1', number=5, relative_path='report.html')
    assert result == 'https://jenkins.example.com/job/job1/5/artifact/report.html'


@pytest.mark.asyncio
async def test_get_build_history(mock_jenkins, mocker):
    mock_jenkins.get_build_history.return_value = [
        Build(number=2, url='u2', result='SUCCESS', timestamp=2, duration=10, building=False),
        Build(number=1, url='u1', result='FAILURE', timestamp=1, duration=20, building=False),
    ]

    result = await build.get_build_history(mocker.Mock(), fullname='job1', count=5)
    assert [b['number'] for b in result] == [2, 1]
    mock_jenkins.get_build_history.assert_called_once_with(fullname='job1', count=5)


@pytest.mark.asyncio
async def test_get_build_stages_with_number(mock_jenkins, mocker):
    mock_jenkins.get_build_stages.return_value = PipelineStages(
        name='#5',
        status='FAILED',
        stages=[PipelineStage(id='10', name='Build', status='SUCCESS', durationMillis=100)],
    )

    result = await build.get_build_stages(mocker.Mock(), fullname='job1', number=5)
    assert result['status'] == 'FAILED'
    assert result['stages'][0]['name'] == 'Build'
    mock_jenkins.get_item.assert_not_called()
    mock_jenkins.get_build_stages.assert_called_once_with(fullname='job1', number=5)


@pytest.mark.asyncio
async def test_get_build_stages_resolves_last_build(mock_jenkins, mocker):
    mock_jenkins.get_item.return_value.lastBuild.number = 7
    mock_jenkins.get_build_stages.return_value = PipelineStages(stages=[])

    await build.get_build_stages(mocker.Mock(), fullname='job1')
    mock_jenkins.get_build_stages.assert_called_once_with(fullname='job1', number=7)


@pytest.mark.asyncio
async def test_get_build_stages_no_build(mock_jenkins, mocker):
    mock_jenkins.get_item.return_value.lastBuild.number = None

    with pytest.raises(ValueError, match='No build found for job: job1'):
        await build.get_build_stages(mocker.Mock(), fullname='job1')


@pytest.mark.asyncio
async def test_get_build_changeset(mock_jenkins, mocker):
    mock_jenkins.get_item.return_value.lastBuild.number = 3
    mock_jenkins.get_build_changeset.return_value = [
        ChangeSetItem(commitId='abc', author='Jane', msg='fix', timestamp=1, affectedPaths=['a.py']),
    ]

    result = await build.get_build_changeset(mocker.Mock(), fullname='job1')
    assert result == [{'commitId': 'abc', 'author': 'Jane', 'msg': 'fix', 'timestamp': 1, 'affectedPaths': ['a.py']}]
    mock_jenkins.get_build_changeset.assert_called_once_with(fullname='job1', number=3)


@pytest.mark.asyncio
async def test_get_build_failure_summary_filters_and_dedupes(mock_jenkins, mocker):
    mock_jenkins.get_build.return_value = Build(number=10, url='u/10', result='UNSTABLE', building=False, timestamp=1)
    mock_jenkins.get_build_stages.return_value = PipelineStages(
        stages=[PipelineStage(id='1', name='mtr', status='SUCCESS')]
    )
    mock_jenkins.get_build_test_report.return_value = {
        'failCount': 2,
        'passCount': 100,
        'skipCount': 1,
        'suites': [
            {
                'name': 'worker_1',
                'cases': [
                    {'className': 'main', 'name': 'pass_test', 'status': 'PASSED', 'duration': 1.0},
                    {
                        'className': 'main',
                        'name': 'dd_upgrade',
                        'status': 'FAILED',
                        'duration': 2.0,
                        'errorDetails': 'boom',
                    },
                    {
                        'className': 'main',
                        'name': 'dd_upgrade',
                        'status': 'FAILED',
                        'duration': 2.0,
                        'errorDetails': 'boom',
                    },
                ],
            },
            {
                'name': 'worker_2',
                'cases': [
                    {'className': 'rpl', 'name': 'rpl_x', 'status': 'REGRESSION', 'duration': 3.0},
                    {'className': 'main', 'name': 'skipped_t', 'status': 'SKIPPED'},
                ],
            },
        ],
    }

    out = await build.get_build_failure_summary(mocker.Mock(), fullname='asan-mtr', number=10)

    assert out['build']['result'] == 'UNSTABLE'
    assert out['test_counts']['failCount'] == 2
    assert out['test_counts']['passCount'] == 100
    assert out['test_counts']['status_counts']['FAILED'] == 2  # raw tally counts the duplicate row
    assert sorted(c['name'] for c in out['cases']) == ['dd_upgrade', 'rpl_x']  # FAILED+REGRESSION, deduped
    assert out['included_count'] == 2
    assert out['stages'][0]['name'] == 'mtr'
    assert {g['suite'] for g in out['groups']} == {'worker_1', 'worker_2'}


@pytest.mark.asyncio
async def test_get_build_failure_summary_status_filter_and_cap(mock_jenkins, mocker):
    mock_jenkins.get_build.return_value = Build(number=5, url='u', result='UNSTABLE')
    mock_jenkins.get_build_stages.return_value = PipelineStages(stages=[])
    mock_jenkins.get_build_test_report.return_value = {
        'failCount': 3,
        'passCount': 0,
        'skipCount': 0,
        'suites': [{'name': 's', 'cases': [{'className': 'c', 'name': f't{i}', 'status': 'FAILED'} for i in range(3)]}],
    }

    out = await build.get_build_failure_summary(
        mocker.Mock(), fullname='j', number=5, status_filter='FAILED', max_cases=2
    )
    assert out['included_count'] == 2
    assert out['truncated'] is True


@pytest.mark.asyncio
async def test_get_build_failure_summary_no_report(mock_jenkins, mocker):
    from requests.exceptions import HTTPError

    mock_jenkins.get_build.return_value = Build(number=1, url='u', result='SUCCESS')
    mock_jenkins.get_build_stages.return_value = PipelineStages(stages=[])
    mock_jenkins.get_build_test_report.side_effect = HTTPError(response=mocker.Mock(status_code=404))

    out = await build.get_build_failure_summary(mocker.Mock(), fullname='j', number=1)
    assert out['cases'] == []
    assert out['test_counts']['failCount'] == 0
    assert any('no test report' in n for n in out['notes'])
    assert out['partial'] is False


@pytest.mark.asyncio
async def test_get_build_failure_summary_flags_unit_tests_wrapper(mock_jenkins, mocker):
    # MTR --unit-tests-report folds a whole ctest run into ONE JUnit case (suite/className end
    # ".report", name "unit_tests"), so failCount=1 hides the real per-test failures. The summary must
    # flag this as partial and steer to the console, not silently undercount (ps80 #1270).
    mock_jenkins.get_build.return_value = Build(number=1270, url='u', result='UNSTABLE', timestamp=1)
    mock_jenkins.get_build_stages.return_value = PipelineStages(stages=[])
    mock_jenkins.get_build_test_report.return_value = {
        'failCount': 1,
        'passCount': 533,
        'skipCount': 10,
        'suites': [
            {
                'name': 'ubuntu-noble.Debug.WORKER_1.UNIT_TESTS.report',
                'cases': [
                    {
                        'className': 'ubuntu-noble.Debug.WORKER_1.UNIT_TESTS.report',
                        'name': 'unit_tests',
                        'status': 'FAILED',
                        'errorDetails': 'Test failed',
                    },
                ],
            },
        ],
    }

    out = await build.get_build_failure_summary(mocker.Mock(), fullname='ps-8.4-mtr', number=1270)

    assert out['partial'] is True
    assert out['wrapper_cases'] == [
        {
            'name': 'unit_tests',
            'className': 'ubuntu-noble.Debug.WORKER_1.UNIT_TESTS.report',
            'suite': 'ubuntu-noble.Debug.WORKER_1.UNIT_TESTS.report',
        }
    ]
    assert out['see_also'] == ['get_build_console_tail', 'grep_build_artifact']
    assert any('understates reality' in n for n in out['notes'])


@pytest.mark.asyncio
async def test_get_build_failure_summary_not_partial_for_normal_failures(mock_jenkins, mocker):
    # A normal per-test failure must NOT be flagged partial and must carry no wrapper_cases/see_also.
    mock_jenkins.get_build.return_value = Build(number=5, url='u', result='UNSTABLE')
    mock_jenkins.get_build_stages.return_value = PipelineStages(stages=[])
    mock_jenkins.get_build_test_report.return_value = {
        'failCount': 1,
        'passCount': 1,
        'skipCount': 0,
        'suites': [{'name': 'worker_1', 'cases': [{'className': 'main', 'name': 'dd_upgrade', 'status': 'FAILED'}]}],
    }

    out = await build.get_build_failure_summary(mocker.Mock(), fullname='j', number=5)
    assert out['partial'] is False
    assert 'wrapper_cases' not in out
    assert 'see_also' not in out


@pytest.mark.asyncio
async def test_get_build_console_tail_passes_and_caps_lines(mock_jenkins, mocker):
    mock_jenkins.get_build_console_tail.return_value = 'line99\nFinished: UNSTABLE'

    out = await build.get_build_console_tail(mocker.Mock(), fullname='j', number=10, lines=2)
    assert out == 'line99\nFinished: UNSTABLE'
    assert mock_jenkins.get_build_console_tail.call_args.kwargs['lines'] == 2

    await build.get_build_console_tail(mocker.Mock(), fullname='j', number=10, lines=999999)
    assert mock_jenkins.get_build_console_tail.call_args.kwargs['lines'] == 5000


@pytest.mark.asyncio
async def test_get_build_console_output_rejects_long_pattern(mock_jenkins, mocker):
    with pytest.raises(ValueError, match='pattern too long'):
        await build.get_build_console_output(mocker.Mock(), fullname='j', number=1, pattern='x' * 2001)


@pytest.mark.asyncio
async def test_get_build_history_caps_count(mock_jenkins, mocker):
    mock_jenkins.get_build_history.return_value = []
    await build.get_build_history(mocker.Mock(), fullname='j', count=99999)
    assert mock_jenkins.get_build_history.call_args.kwargs['count'] == 100
