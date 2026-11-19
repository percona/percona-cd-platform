import threading

import pytest

from mcp_jenkins.jenkins.model.build import Artifact, Build, BuildReplay, ChangeSetItem, PipelineStage, PipelineStages
from mcp_jenkins.jenkins.rest_client import PayloadTooLargeError
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

    assert build.get_running_builds(mocker.Mock()) == [
        {'number': 1, 'url': '1', 'building': True, 'timestamp': 1234567890},
        {'number': 2, 'url': '2', 'building': True, 'timestamp': 1234567891},
    ]


@pytest.mark.asyncio
async def test_get_build(mock_jenkins, mocker):
    mock_jenkins.get_item.return_value.lastBuild.number = 1
    mock_jenkins.get_build.return_value = Build(number=1, url='1', building=False, timestamp=1234567890)

    assert build.get_build(mocker.Mock(), fullname='job1') == {
        'number': 1,
        'url': '1',
        'building': False,
        'timestamp': 1234567890,
    }


@pytest.mark.asyncio
async def test_get_build_scripts(mock_jenkins, mocker):
    mock_jenkins.get_item.return_value.lastBuild.number = 1
    mock_jenkins.get_build_replay.return_value = BuildReplay(scripts=['script1', 'script2'])

    assert build.get_build_scripts(mocker.Mock(), fullname='job1') == [
        'script1',
        'script2',
    ]


@pytest.mark.asyncio
async def test_get_build_console_output(mock_jenkins, mocker):
    mock_jenkins.get_item.return_value.lastBuild.number = 1
    mock_jenkins.get_build_console_output.return_value = 'Console output here'

    assert build.get_build_console_output(mocker.Mock(), fullname='job1') == 'Console output here'
    mock_jenkins.get_build_console_output.assert_called_once_with(
        fullname='job1', number=1, pattern=None, offset=0, limit=None
    )


@pytest.mark.asyncio
async def test_get_build_console_output_with_number(mock_jenkins, mocker):
    mock_jenkins.get_build_console_output.return_value = 'output'

    assert build.get_build_console_output(mocker.Mock(), fullname='job1', number=5) == 'output'
    mock_jenkins.get_item.assert_not_called()
    mock_jenkins.get_build_console_output.assert_called_once_with(
        fullname='job1', number=5, pattern=None, offset=0, limit=None
    )


@pytest.mark.asyncio
async def test_get_build_console_output_with_all_params(mock_jenkins, mocker):
    mock_jenkins.get_build_console_output.return_value = 'ERROR: boom'

    result = build.get_build_console_output(
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
        build.get_build_console_output(mocker.Mock(), fullname='job1')


@pytest.mark.asyncio
async def test_get_build_test_reports(mock_jenkins, mocker):
    mock_jenkins.get_item.return_value.lastBuild.number = 1
    mock_jenkins.get_build_test_report.return_value = {'failCount': 0, 'suites': [{'name': 's', 'cases': [{}]}]}

    assert build.get_build_test_report(mocker.Mock(), fullname='job1') == {
        'failCount': 0,
        'suites': [{'name': 's', 'cases': [{}]}],
        'cases_total': 1,
        'cases_returned': 1,
        'truncated': False,
    }
    mock_jenkins.get_build_test_report.assert_called_once_with(fullname='job1', number=1, tree=build._TEST_REPORT_TREE)


def test_test_report_tree_leaves_out_stdout_and_stderr():
    assert 'stdout' not in build._TEST_REPORT_TREE
    assert 'stderr' not in build._TEST_REPORT_TREE
    assert 'errorStackTrace' in build._TEST_REPORT_TREE


def _report_with(cases_per_suite: list[int]) -> dict:
    return {
        'suites': [
            {'name': f's{suite}', 'cases': [{'name': f'c{suite}.{case}'} for case in range(count)]}
            for suite, count in enumerate(cases_per_suite)
        ]
    }


@pytest.mark.parametrize(
    ('max_cases', 'kept', 'truncated'),
    [(5, 5, False), (4, 4, True), (6, 5, False), (1, 1, True), (5000, 5, False)],
)
def test_test_report_caps_cases_across_suites(mock_jenkins, mocker, max_cases, kept, truncated):
    mock_jenkins.get_build_test_report.return_value = _report_with([3, 0, 2])
    out = build.get_build_test_report(mocker.Mock(), fullname='job1', number=1, max_cases=max_cases)
    assert sum(len(suite['cases']) for suite in out['suites']) == kept
    assert (out['cases_total'], out['cases_returned'], out['truncated']) == (5, kept, truncated)
    assert ('note' in out) is truncated


def test_test_report_case_cap_is_bounded(mock_jenkins, mocker):
    mock_jenkins.get_build_test_report.return_value = _report_with([1500])
    out = build.get_build_test_report(mocker.Mock(), fullname='job1', number=1, max_cases=99999)
    assert out['cases_returned'] == build._MAX_FAILURE_CASES
    assert 'get_build_failure_summary' in out['note']


def test_test_report_cuts_long_error_text(mock_jenkins, mocker):
    case = {'name': 'c', 'errorDetails': 'd' * 5000, 'errorStackTrace': 't' * 5000}
    mock_jenkins.get_build_test_report.return_value = {'suites': [{'name': 's', 'cases': [case]}]}
    out = build.get_build_test_report(mocker.Mock(), fullname='job1', number=1)
    kept = out['suites'][0]['cases'][0]
    assert len(kept['errorDetails']) == len(kept['errorStackTrace']) == build._MAX_REPORT_TEXT_CHARS


def test_get_build_artifact_refusal_reaches_the_caller(mock_jenkins, mocker):
    refusal = PayloadTooLargeError('inline limit of 10 MiB, use export_build_artifact')
    mock_jenkins.get_build_artifact.side_effect = refusal
    with pytest.raises(ValueError, match='export_build_artifact'):
        build.get_build_artifact(mocker.Mock(), fullname='job1', relative_path='big.bin', number=1)


@pytest.mark.asyncio
async def test_get_build_parameters(mock_jenkins, mocker):
    mock_jenkins.get_item.return_value.lastBuild.number = 1
    mock_jenkins.get_build_parameters.return_value = {'BRANCH': 'main', 'DEBUG': True}

    assert build.get_build_parameters(mocker.Mock(), fullname='job1') == {
        'BRANCH': 'main',
        'DEBUG': True,
    }


@pytest.mark.asyncio
async def test_stop_build(mock_jenkins, mocker):
    build.stop_build(mocker.Mock(), fullname='job1', number=1)
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

    assert build.get_all_build_artifacts(mocker.Mock(), fullname='job1') == [
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

    assert build.get_all_build_artifacts(mocker.Mock(), fullname='job1', number=5) == []
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

    out = build.get_all_build_artifacts(mocker.Mock(), fullname='job1', number=1270)
    hints = {a['relativePath']: a.get('hint') for a in out}
    assert hints['build.log.gz'] == 'full build console log'
    assert hints['work/make_build.log'] == 'build log'
    assert hints['work/mtr-test_1.log'].startswith('test-runner log')
    assert hints['work/results/ps80-test-mtr_logs-1.tar.gz'].startswith('per-test logs archive')
    assert hints['public_url'] is None


@pytest.mark.asyncio
async def test_get_all_build_artifacts_non_log_gz_unlabeled(mock_jenkins, mocker):
    # A bare .gz that is not a .log.gz must not be mislabeled as a log.
    mock_jenkins.get_build_artifacts.return_value = [
        Artifact(fileName='core.gz', relativePath='work/core.gz'),
        Artifact(fileName='dump.sql.gz', relativePath='work/dump.sql.gz'),
    ]

    out = build.get_all_build_artifacts(mocker.Mock(), fullname='job1', number=1)
    assert all('hint' not in a for a in out)


@pytest.mark.asyncio
async def test_get_build_artifact_text(mock_jenkins, mocker):
    mock_jenkins.get_item.return_value.lastBuild.number = 1
    mock_jenkins.get_build_artifact.return_value = b'<html>report</html>'

    result = build.get_build_artifact(mocker.Mock(), fullname='job1', relative_path='playwright-report/index.html')
    assert result == {'content': '<html>report</html>', 'encoding': 'utf-8'}


@pytest.mark.asyncio
async def test_get_build_artifact_binary(mock_jenkins, mocker):
    mock_jenkins.get_item.return_value.lastBuild.number = 1
    mock_jenkins.get_build_artifact.return_value = bytes(range(256))

    result = build.get_build_artifact(mocker.Mock(), fullname='job1', relative_path='trace.zip')
    assert result['encoding'] == 'base64'
    import base64

    assert base64.b64decode(result['content']) == bytes(range(256))


@pytest.mark.asyncio
async def test_get_build_artifact_with_number(mock_jenkins, mocker):
    mock_jenkins.get_build_artifact.return_value = b'data'

    result = build.get_build_artifact(mocker.Mock(), fullname='job1', relative_path='file.txt', number=3)
    mock_jenkins.get_item.assert_not_called()
    mock_jenkins.get_build_artifact.assert_called_once_with(fullname='job1', number=3, relative_path='file.txt')
    assert result == {'content': 'data', 'encoding': 'utf-8'}


@pytest.mark.asyncio
async def test_get_build_artifact_url(mock_jenkins, mocker):
    mock_jenkins.get_item.return_value.lastBuild.number = 1
    mock_jenkins.get_build_artifact_url.return_value = 'https://jenkins.example.com/job/job1/1/artifact/trace.zip'

    result = build.get_build_artifact_url(mocker.Mock(), fullname='job1', relative_path='trace.zip')
    assert result == 'https://jenkins.example.com/job/job1/1/artifact/trace.zip'


@pytest.mark.asyncio
async def test_get_build_artifact_url_with_number(mock_jenkins, mocker):
    mock_jenkins.get_build_artifact_url.return_value = 'https://jenkins.example.com/job/job1/5/artifact/report.html'

    result = build.get_build_artifact_url(mocker.Mock(), fullname='job1', relative_path='report.html', number=5)
    mock_jenkins.get_item.assert_not_called()
    mock_jenkins.get_build_artifact_url.assert_called_once_with(fullname='job1', number=5, relative_path='report.html')
    assert result == 'https://jenkins.example.com/job/job1/5/artifact/report.html'


_UNSAFE_RELATIVE_PATHS = ['../../config.xml', 'a/../b', '/abs', 'x?y', '%2e%2e/x']


@pytest.mark.asyncio
@pytest.mark.parametrize('tool', ['get_build_artifact', 'get_build_artifact_url'])
@pytest.mark.parametrize('relative_path', _UNSAFE_RELATIVE_PATHS)
async def test_artifact_tools_reject_unsafe_path(mock_jenkins, mocker, tool, relative_path):
    with pytest.raises(ValueError):
        await getattr(build, tool)(mocker.Mock(), fullname='job1', relative_path=relative_path, number=3)
    getattr(mock_jenkins, tool).assert_not_called()
    mock_jenkins.get_item.assert_not_called()


@pytest.mark.asyncio
@pytest.mark.parametrize('tool', ['get_build_artifact', 'get_build_artifact_url'])
@pytest.mark.parametrize('relative_path', _UNSAFE_RELATIVE_PATHS)
async def test_artifact_tools_reject_unsafe_path_before_number_lookup(mock_jenkins, mocker, tool, relative_path):
    with pytest.raises(ValueError):
        await getattr(build, tool)(mocker.Mock(), fullname='job1', relative_path=relative_path)
    mock_jenkins.get_item.assert_not_called()
    getattr(mock_jenkins, tool).assert_not_called()


@pytest.mark.asyncio
async def test_get_build_artifact_keeps_plain_names(mock_jenkins, mocker):
    mock_jenkins.get_build_artifact.return_value = b'data'

    build.get_build_artifact(mocker.Mock(), fullname='job1', relative_path='out/big.bin', number=3)
    mock_jenkins.get_build_artifact.assert_called_once_with(fullname='job1', number=3, relative_path='out/big.bin')


@pytest.mark.asyncio
async def test_get_build_history(mock_jenkins, mocker):
    mock_jenkins.get_build_history.return_value = [
        Build(number=2, url='u2', result='SUCCESS', timestamp=2, duration=10, building=False),
        Build(number=1, url='u1', result='FAILURE', timestamp=1, duration=20, building=False),
    ]

    result = build.get_build_history(mocker.Mock(), fullname='job1', count=5)
    assert [b['number'] for b in result] == [2, 1]
    mock_jenkins.get_build_history.assert_called_once_with(fullname='job1', count=5)


@pytest.mark.asyncio
async def test_get_build_stages_with_number(mock_jenkins, mocker):
    mock_jenkins.get_build_stages.return_value = PipelineStages(
        name='#5',
        status='FAILED',
        stages=[PipelineStage(id='10', name='Build', status='SUCCESS', durationMillis=100)],
    )

    result = build.get_build_stages(mocker.Mock(), fullname='job1', number=5)
    assert result['status'] == 'FAILED'
    assert result['stages'][0]['name'] == 'Build'
    mock_jenkins.get_item.assert_not_called()
    mock_jenkins.get_build_stages.assert_called_once_with(fullname='job1', number=5)


@pytest.mark.asyncio
async def test_get_build_stages_resolves_last_build(mock_jenkins, mocker):
    mock_jenkins.get_item.return_value.lastBuild.number = 7
    mock_jenkins.get_build_stages.return_value = PipelineStages(stages=[])

    build.get_build_stages(mocker.Mock(), fullname='job1')
    mock_jenkins.get_build_stages.assert_called_once_with(fullname='job1', number=7)


@pytest.mark.asyncio
async def test_get_build_stages_no_build(mock_jenkins, mocker):
    mock_jenkins.get_item.return_value.lastBuild.number = None

    with pytest.raises(ValueError, match='No build found for job: job1'):
        build.get_build_stages(mocker.Mock(), fullname='job1')


@pytest.mark.asyncio
async def test_get_build_changeset(mock_jenkins, mocker):
    mock_jenkins.get_item.return_value.lastBuild.number = 3
    mock_jenkins.get_build_changeset.return_value = [
        ChangeSetItem(commitId='abc', author='Jane', msg='fix', timestamp=1, affectedPaths=['a.py']),
    ]

    result = build.get_build_changeset(mocker.Mock(), fullname='job1')
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

    out = build.get_build_failure_summary(mocker.Mock(), fullname='asan-mtr', number=10)

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

    out = build.get_build_failure_summary(mocker.Mock(), fullname='j', number=5, status_filter='FAILED', max_cases=2)
    assert out['included_count'] == 2
    assert out['truncated'] is True


@pytest.mark.asyncio
async def test_get_build_failure_summary_no_report(mock_jenkins, mocker):
    from requests.exceptions import HTTPError

    mock_jenkins.get_build.return_value = Build(number=1, url='u', result='SUCCESS')
    mock_jenkins.get_build_stages.return_value = PipelineStages(stages=[])
    mock_jenkins.get_build_test_report.side_effect = HTTPError(response=mocker.Mock(status_code=404))

    out = build.get_build_failure_summary(mocker.Mock(), fullname='j', number=1)
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

    out = build.get_build_failure_summary(mocker.Mock(), fullname='ps-8.4-mtr', number=1270)

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

    out = build.get_build_failure_summary(mocker.Mock(), fullname='j', number=5)
    assert out['partial'] is False
    assert 'wrapper_cases' not in out
    assert 'see_also' not in out


@pytest.mark.asyncio
async def test_get_build_failure_summary_wrapper_detected_despite_status_filter(mock_jenkins, mocker):
    # The wrapper is FAILED; status_filter=REGRESSION drops it from the displayed `cases`, but partial
    # must still be flagged because the build DOES hide failures (raw-report detection, not filtered).
    mock_jenkins.get_build.return_value = Build(number=1, url='u', result='UNSTABLE')
    mock_jenkins.get_build_stages.return_value = PipelineStages(stages=[])
    mock_jenkins.get_build_test_report.return_value = {
        'failCount': 1,
        'passCount': 0,
        'skipCount': 0,
        'suites': [
            {
                'name': 'w1.UNIT_TESTS.report',
                'cases': [
                    {'className': 'w1.UNIT_TESTS.report', 'name': 'unit_tests', 'status': 'FAILED'},
                    {'className': 'rpl', 'name': 'rpl_x', 'status': 'REGRESSION'},
                ],
            },
        ],
    }

    out = build.get_build_failure_summary(mocker.Mock(), fullname='j', number=1, status_filter='REGRESSION')
    assert out['partial'] is True
    assert [w['name'] for w in out['wrapper_cases']] == ['unit_tests']
    assert [c['name'] for c in out['cases']] == ['rpl_x']  # FAILED wrapper excluded from display by the filter


@pytest.mark.asyncio
async def test_get_build_failure_summary_passing_wrapper_not_partial(mock_jenkins, mocker):
    # A PASSING rolled-up report hides nothing; status_filter=ALL includes it in cases, but it must
    # NOT set partial (only FAILED/REGRESSION wrappers are load-bearing).
    mock_jenkins.get_build.return_value = Build(number=1, url='u', result='SUCCESS')
    mock_jenkins.get_build_stages.return_value = PipelineStages(stages=[])
    mock_jenkins.get_build_test_report.return_value = {
        'failCount': 0,
        'passCount': 1,
        'skipCount': 0,
        'suites': [
            {
                'name': 'w1.UNIT_TESTS.report',
                'cases': [{'className': 'w1.UNIT_TESTS.report', 'name': 'unit_tests', 'status': 'PASSED'}],
            },
        ],
    }

    out = build.get_build_failure_summary(mocker.Mock(), fullname='j', number=1, status_filter='ALL')
    assert out['partial'] is False
    assert 'wrapper_cases' not in out


@pytest.mark.asyncio
async def test_get_build_failure_summary_unit_tests_substring_not_wrapper(mock_jenkins, mocker):
    # A real failing test under a `unit_tests` package must NOT be misread as a rollup wrapper.
    mock_jenkins.get_build.return_value = Build(number=1, url='u', result='UNSTABLE')
    mock_jenkins.get_build_stages.return_value = PipelineStages(stages=[])
    mock_jenkins.get_build_test_report.return_value = {
        'failCount': 1,
        'passCount': 0,
        'skipCount': 0,
        'suites': [
            {
                'name': 'storage.unit_tests.btree',
                'cases': [{'className': 'storage.unit_tests.btree', 'name': 'insert_split', 'status': 'FAILED'}],
            },
        ],
    }

    out = build.get_build_failure_summary(mocker.Mock(), fullname='j', number=1)
    assert out['partial'] is False
    assert 'wrapper_cases' not in out


@pytest.mark.asyncio
async def test_get_build_console_tail_passes_and_caps_lines(mock_jenkins, mocker):
    mock_jenkins.get_build_console_tail.return_value = 'line99\nFinished: UNSTABLE'

    out = build.get_build_console_tail(mocker.Mock(), fullname='j', number=10, lines=2)
    assert out == 'line99\nFinished: UNSTABLE'
    assert mock_jenkins.get_build_console_tail.call_args.kwargs['lines'] == 2

    build.get_build_console_tail(mocker.Mock(), fullname='j', number=10, lines=999999)
    assert mock_jenkins.get_build_console_tail.call_args.kwargs['lines'] == 5000


@pytest.mark.asyncio
async def test_get_build_console_output_rejects_long_pattern(mock_jenkins, mocker):
    with pytest.raises(ValueError, match='pattern too long'):
        build.get_build_console_output(mocker.Mock(), fullname='j', number=1, pattern='x' * 2001)


@pytest.mark.asyncio
async def test_get_build_history_caps_count(mock_jenkins, mocker):
    mock_jenkins.get_build_history.return_value = []
    build.get_build_history(mocker.Mock(), fullname='j', count=99999)
    assert mock_jenkins.get_build_history.call_args.kwargs['count'] == 100


@pytest.mark.asyncio
async def test_get_build_history_accepts_limit_alias(mock_jenkins, mocker):
    mock_jenkins.get_build_history.return_value = []

    build.get_build_history(mocker.Mock(), fullname='job1', limit=3)

    mock_jenkins.get_build_history.assert_called_once_with(fullname='job1', count=3)


# ---- a report over the read limit ----
def test_test_report_tool_adds_the_pointer_to_a_refusal(mock_jenkins, mocker):
    mock_jenkins.get_build_test_report.side_effect = PayloadTooLargeError('the test report is larger than X')
    with pytest.raises(PayloadTooLargeError, match='larger than X') as refused:
        build.get_build_test_report(mocker.Mock(), fullname='j', number=1)
    assert 'get_build_failure_summary' in str(refused.value)


def test_failure_summary_degrades_when_the_report_is_too_large(mock_jenkins, mocker):
    mock_jenkins.get_build.return_value = Build(number=1, url='u', result='UNSTABLE')
    mock_jenkins.get_build_stages.return_value = PipelineStages(stages=[])
    mock_jenkins.get_build_test_report.side_effect = PayloadTooLargeError(
        'the test report is larger than the 32 MiB read limit'
    )
    out = build.get_build_failure_summary(mocker.Mock(), fullname='j', number=1)
    assert out['cases'] == []
    assert out['test_counts']['failCount'] == 0
    note = next(n for n in out['notes'] if 'read limit' in n)
    assert 'get_build_console_tail' in note
    assert 'grep_build_artifact' in note
    assert 'get_build_failure_summary' not in note
    assert out['see_also'] == ['get_build_console_tail', 'grep_build_artifact']
    assert build._REPORT_READ_SLOTS._value == 2


# ---- aggregated (matrix) reports ----
def _matrix_report() -> dict:
    def child(number: int, cases: list[dict]) -> dict:
        return {
            'child': {'number': number, 'url': f'https://m/job/x/axis={number}/{number}/'},
            'result': {'failCount': 1, 'suites': [{'name': f's{number}', 'cases': cases}]},
        }

    return {
        'failCount': 2,
        'totalCount': 6,
        'childReports': [
            child(1, [{'name': 'a1'}, {'name': 'a2'}]),
            child(2, [{'name': 'b1'}, {'name': 'b2', 'errorStackTrace': 't' * 5000}, {'name': 'b3'}]),
            child(3, [{'name': 'c1', 'errorDetails': 'd' * 5000}]),
        ],
    }


def _child_cases(out: dict) -> list[list[str]]:
    return [
        [case['name'] for suite in child['result']['suites'] for case in suite['cases']]
        for child in out['childReports']
    ]


def test_test_report_tree_keeps_child_reports():
    assert 'childReports[child[number,url],result[' in build._TEST_REPORT_TREE
    assert build._TEST_REPORT_TREE.count('errorStackTrace') == 2


def test_matrix_report_under_the_cap_is_whole(mock_jenkins, mocker):
    mock_jenkins.get_build_test_report.return_value = _matrix_report()
    out = build.get_build_test_report(mocker.Mock(), fullname='j', number=1, max_cases=10)
    assert _child_cases(out) == [['a1', 'a2'], ['b1', 'b2', 'b3'], ['c1']]
    assert (out['cases_total'], out['cases_returned'], out['truncated']) == (6, 6, False)
    assert out['childReports'][0]['child']['number'] == 1


def test_matrix_report_over_the_cap_cuts_inside_the_second_child(mock_jenkins, mocker):
    mock_jenkins.get_build_test_report.return_value = _matrix_report()
    out = build.get_build_test_report(mocker.Mock(), fullname='j', number=1, max_cases=3)
    assert _child_cases(out) == [['a1', 'a2'], ['b1'], []]
    assert (out['cases_total'], out['cases_returned'], out['truncated']) == (6, 3, True)
    assert out['note'].startswith('3 of 6 cases are left out')


def test_matrix_report_error_texts_are_cut_inside_a_child(mock_jenkins, mocker):
    mock_jenkins.get_build_test_report.return_value = _matrix_report()
    out = build.get_build_test_report(mocker.Mock(), fullname='j', number=1)
    cases = [case for child in out['childReports'] for suite in child['result']['suites'] for case in suite['cases']]
    by_name = {case['name']: case for case in cases}
    assert len(by_name['b2']['errorStackTrace']) == build._MAX_REPORT_TEXT_CHARS
    assert len(by_name['c1']['errorDetails']) == build._MAX_REPORT_TEXT_CHARS


# ---- concurrent large reads ----
def _blocking_artifact(mock_jenkins):
    """Make get_build_artifact wait on a gate, and count the calls that entered it."""
    gate = threading.Event()
    entered = threading.Semaphore(0)

    def read(**_kwargs: object) -> bytearray:
        entered.release()
        assert gate.wait(10)
        return bytearray(b'ok')

    mock_jenkins.get_build_artifact.side_effect = read
    return gate, entered


def _read_artifact_in_thread(mocker, results: list) -> threading.Thread:
    def run() -> None:
        try:
            results.append(build.get_build_artifact(mocker.Mock(), fullname='j', relative_path='a.txt', number=1))
        except ValueError as e:
            results.append(e)

    thread = threading.Thread(target=run)
    thread.start()
    return thread


def test_fifth_artifact_read_waits_for_a_free_slot(mock_jenkins, mocker, monkeypatch):
    monkeypatch.setattr(build, '_ARTIFACT_READ_SLOTS', threading.BoundedSemaphore(4))
    gate, entered = _blocking_artifact(mock_jenkins)
    results: list = []
    threads = [_read_artifact_in_thread(mocker, results) for _ in range(4)]
    for _ in range(4):
        assert entered.acquire(timeout=5)
    fifth = _read_artifact_in_thread(mocker, results)
    assert not entered.acquire(timeout=0.3)  # the fifth read waits outside the client
    gate.set()
    for thread in [*threads, fifth]:
        thread.join(5)
    assert results == [{'content': 'ok', 'encoding': 'utf-8'}] * 5
    assert mock_jenkins.get_build_artifact.call_count == 5


def test_artifact_read_past_the_wait_limit_gets_the_busy_error(mock_jenkins, mocker, monkeypatch):
    monkeypatch.setattr(build, '_ARTIFACT_READ_SLOTS', threading.BoundedSemaphore(1))
    monkeypatch.setattr(build, '_LARGE_READ_WAIT_SECONDS', 0.2)
    gate, entered = _blocking_artifact(mock_jenkins)
    results: list = []
    first = _read_artifact_in_thread(mocker, results)
    assert entered.acquire(timeout=5)
    with pytest.raises(ValueError, match='busy with other large artifact reads. Retry'):
        build.get_build_artifact(mocker.Mock(), fullname='j', relative_path='a.txt', number=1)
    gate.set()
    first.join(5)
    assert results == [{'content': 'ok', 'encoding': 'utf-8'}]


def test_artifact_slot_is_released_on_an_error(mock_jenkins, mocker, monkeypatch):
    slots = threading.BoundedSemaphore(1)
    monkeypatch.setattr(build, '_ARTIFACT_READ_SLOTS', slots)
    monkeypatch.setattr(build, '_LARGE_READ_WAIT_SECONDS', 0.2)
    mock_jenkins.get_build_artifact.side_effect = PayloadTooLargeError('too large')
    for _ in range(3):
        with pytest.raises(PayloadTooLargeError):
            build.get_build_artifact(mocker.Mock(), fullname='j', relative_path='a.txt', number=1)
    assert slots._value == 1


def test_report_slot_is_released_on_an_error(mock_jenkins, mocker, monkeypatch):
    slots = threading.BoundedSemaphore(1)
    monkeypatch.setattr(build, '_REPORT_READ_SLOTS', slots)
    monkeypatch.setattr(build, '_LARGE_READ_WAIT_SECONDS', 0.2)
    mock_jenkins.get_build_test_report.side_effect = PayloadTooLargeError('too large')
    for _ in range(3):
        with pytest.raises(PayloadTooLargeError):
            build.get_build_test_report(mocker.Mock(), fullname='j', number=1)
    assert slots._value == 1


def test_report_reads_wait_on_their_own_slots(mock_jenkins, mocker, monkeypatch):
    monkeypatch.setattr(build, '_REPORT_READ_SLOTS', threading.BoundedSemaphore(1))
    monkeypatch.setattr(build, '_LARGE_READ_WAIT_SECONDS', 0.2)
    with build._large_read(build._REPORT_READ_SLOTS, 'test report'):
        with pytest.raises(ValueError, match='busy with other large test report reads'):
            build.get_build_test_report(mocker.Mock(), fullname='j', number=1)
        mock_jenkins.get_build_test_report.assert_not_called()
