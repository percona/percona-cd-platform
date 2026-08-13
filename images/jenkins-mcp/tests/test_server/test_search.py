import asyncio
import inspect
import threading
import time

import pytest
from fastmcp import Client
from requests.exceptions import ConnectionError as RequestsConnectionError
from requests.exceptions import HTTPError

from mcp_jenkins.jenkins.model.build import Build
from mcp_jenkins.jenkins.model.item import Folder, Job
from mcp_jenkins.server import mcp, search


@pytest.fixture
def mock_jenkins(mocker):
    mock_jenkins = mocker.Mock()
    mocker.patch('mcp_jenkins.server.search.jenkins', return_value=mock_jenkins)
    # Default history: the newest `count` builds, all finished, anchored on _job()'s build 10.
    mock_jenkins.get_build_history.side_effect = lambda *, fullname, count: [
        Build(number=n, url='u', building=False) for n in range(10, 10 - count, -1)
    ]
    yield mock_jenkins


def _job(fullname, number=10):
    return Job(
        fullname=fullname,
        name=fullname.split('/')[-1],
        url='u',
        class_='Job',
        color='blue',
        lastBuild=Build(number=number, url='u'),
    )


def test_search_happy_path(mock_jenkins, mocker):
    mock_jenkins.query_items.return_value = [_job('pxc-a'), _job('pxc-b')]
    mock_jenkins.get_build_console_output.side_effect = lambda **kw: (
        'ERROR x\nERROR y' if kw['fullname'] == 'pxc-a' else ''
    )

    out = search.search_build_logs(mocker.Mock(), pattern='ERROR', job_pattern='pxc.*')

    assert out['summary']['jobs_scanned'] == 2
    assert out['summary']['jobs_with_matches'] == 1
    assert out['summary']['total_matches'] == 2
    assert out['results'][0] == {'job_fullname': 'pxc-a', 'build_number': 10, 'matching_lines': ['ERROR x', 'ERROR y']}
    mock_jenkins.query_items.assert_called_once_with(fullname_pattern='pxc.*', folder_depth=None)


@pytest.mark.parametrize('bad', ['', '.*', '*', '   '])
def test_search_rejects_catch_all(mock_jenkins, mocker, bad):
    with pytest.raises(ValueError, match='job_pattern'):
        search.search_build_logs(mocker.Mock(), pattern='x', job_pattern=bad)
    mock_jenkins.query_items.assert_not_called()


def test_search_max_jobs_cap(mock_jenkins, mocker):
    mock_jenkins.query_items.return_value = [_job(f'pxc-{i}') for i in range(30)]
    mock_jenkins.get_build_console_output.return_value = ''

    out = search.search_build_logs(mocker.Mock(), pattern='x', job_pattern='pxc.*', max_jobs=5)

    assert out['summary']['jobs_scanned'] == 5
    assert out['summary']['truncated_jobs'] is True
    assert mock_jenkins.get_build_console_output.call_count == 5


def test_search_clamps_and_notes(mock_jenkins, mocker):
    mock_jenkins.query_items.return_value = []

    out = search.search_build_logs(mocker.Mock(), pattern='x', job_pattern='pxc.*', max_jobs=9999)

    assert any('max_jobs adjusted from 9999 to 200' in n for n in out['summary']['notes'])


def test_search_max_total_matches_early_stop(mock_jenkins, mocker):
    mock_jenkins.query_items.return_value = [_job('a'), _job('b'), _job('c')]
    mock_jenkins.get_build_console_output.side_effect = lambda **kw: '\n'.join(f'ERROR {i}' for i in range(kw['limit']))

    out = search.search_build_logs(mocker.Mock(), pattern='ERROR', job_pattern='job.*', max_total_matches=30)

    assert out['summary']['total_matches'] == 30
    assert out['summary']['truncated_matches'] is True
    assert out['summary']['jobs_scanned'] == 2


def test_search_ignore_case_prefixes_pattern(mock_jenkins, mocker):
    mock_jenkins.query_items.return_value = [_job('a')]
    mock_jenkins.get_build_console_output.return_value = 'error'

    search.search_build_logs(mocker.Mock(), pattern='ERROR', job_pattern='a.*', ignore_case=True)

    assert mock_jenkins.get_build_console_output.call_args.kwargs['pattern'] == '(?i)ERROR'


def test_search_skips_folders_and_unbuilt(mock_jenkins, mocker):
    folder = Folder(fullname='dir', name='dir', url='u', class_='Folder', jobs=[])
    unbuilt = Job(fullname='new', name='new', url='u', class_='Job', color='notbuilt')
    mock_jenkins.query_items.return_value = [folder, unbuilt, _job('built')]
    mock_jenkins.get_build_console_output.return_value = 'MATCH'

    out = search.search_build_logs(mocker.Mock(), pattern='MATCH', job_pattern='b.*')

    assert out['summary']['jobs_matched_pattern'] == 3  # all name-matches, incl. folder + unbuilt
    assert out['summary']['jobs_with_last_build'] == 1  # only the built job is scannable
    assert out['summary']['jobs_scanned'] == 1
    assert mock_jenkins.get_build_console_output.call_count == 1


def test_search_rejects_invalid_regex(mock_jenkins, mocker):
    mock_jenkins.query_items.return_value = []
    with pytest.raises(ValueError, match='invalid pattern regex'):
        search.search_build_logs(mocker.Mock(), pattern='(unclosed', job_pattern='pxc.*')
    mock_jenkins.query_items.assert_not_called()


def test_search_builds_per_job_skips_rotated(mock_jenkins, mocker):
    mock_jenkins.query_items.return_value = [_job('a', number=10)]
    calls = []

    def console(**kw: object) -> str:
        calls.append(kw['number'])
        if kw['number'] == 9:
            raise HTTPError(response=mocker.Mock(status_code=404))
        return 'HIT'

    mock_jenkins.get_build_console_output.side_effect = console

    out = search.search_build_logs(mocker.Mock(), pattern='HIT', job_pattern='a.*', builds_per_job=3)

    assert calls == [10, 9, 8]
    assert out['summary']['builds_scanned'] == 2
    assert out['summary']['fetch_errors'] == 1
    assert out['summary']['total_matches'] == 2


def test_search_build_logs_stays_sync_so_fastmcp_offloads_it():
    """An `async def` here would put the whole scan back on the event loop.

    That froze /healthz for the length of the scan and let the kubelet liveness probe SIGKILL the
    container mid-call. FastMCP only moves a tool to a worker thread when the function is sync.
    """
    assert not inspect.iscoroutinefunction(search.search_build_logs)


@pytest.mark.asyncio
async def test_search_through_fastmcp_keeps_the_event_loop_responsive(mock_jenkins, mocker):
    """End-to-end gate: dispatch through the real FastMCP tool path, not the bare function.

    Proves the property that actually keeps the pod alive, and would catch a future FastMCP change
    that stopped offloading sync tools just as surely as an `async def` regression here.
    """
    mock_jenkins.query_items.return_value = [_job(f'slow-{i}') for i in range(6)]
    mock_jenkins.get_build_console_output.side_effect = lambda **kw: time.sleep(0.1) or 'HIT'

    ticks = 0

    async def heartbeat() -> None:
        nonlocal ticks
        while True:  # noqa: ASYNC110 - polls loop liveness, which is the property under test
            await asyncio.sleep(0.005)
            ticks += 1

    beat = asyncio.create_task(heartbeat())
    try:
        async with Client(mcp) as client:
            result = await client.call_tool('search_build_logs', {'pattern': 'HIT', 'job_pattern': 'slow.*'})
    finally:
        beat.cancel()

    assert result.structured_content['summary']['builds_scanned'] == 6
    # Run on the loop (the old behaviour) the heartbeat is frozen for the whole scan and lands at 0.
    assert ticks >= 5


def test_search_time_budget_returns_partial(mock_jenkins, mocker):
    mock_jenkins.query_items.return_value = [_job(f'slow-{i}') for i in range(8)]
    mock_jenkins.get_build_console_output.side_effect = lambda **kw: time.sleep(0.3) or 'HIT'

    out = search.search_build_logs(
        mocker.Mock(), pattern='HIT', job_pattern='slow.*', builds_per_job=5, time_budget_seconds=1
    )

    assert out['summary']['timed_out'] is True
    assert out['summary']['builds_scanned'] < 40  # 8 jobs x 5 builds, stopped on budget
    assert out['summary']['elapsed_seconds'] < 5


def test_search_reports_timeout_when_the_last_fetch_overruns(mock_jenkins, mocker):
    """A budget that expires during the final fetch must still report timed_out."""
    mock_jenkins.query_items.return_value = [_job('slow')]
    mock_jenkins.get_build_console_output.side_effect = lambda **kw: time.sleep(1.2) or 'HIT'

    out = search.search_build_logs(mocker.Mock(), pattern='HIT', job_pattern='slow.*', time_budget_seconds=1)

    assert out['summary']['elapsed_seconds'] >= 1
    assert out['summary']['timed_out'] is True


def test_search_skips_running_builds(mock_jenkins, mocker):
    """A running build's console never ends, so reading one pins a thread until the build finishes."""
    mock_jenkins.query_items.return_value = [_job('a', number=11)]
    mock_jenkins.get_build_history.side_effect = lambda *, fullname, count: [
        Build(number=11, url='u', building=True),
        Build(number=10, url='u', building=False),
    ]
    mock_jenkins.get_build_console_output.return_value = 'HIT'

    out = search.search_build_logs(mocker.Mock(), pattern='HIT', job_pattern='a.*', builds_per_job=2)

    assert mock_jenkins.get_build_console_output.call_args_list[0].kwargs['number'] == 10
    assert mock_jenkins.get_build_console_output.call_count == 1
    assert out['summary']['builds_skipped_running'] == 1


def test_search_abandons_a_wedged_console_read(mock_jenkins, mocker):
    """A console read cannot be cancelled, so the scan must stop waiting on it and report timed_out.

    Jenkins streams a console for as long as it keeps growing and requests' timeout only fires on
    read inactivity, so without a bounded wait one slow log blocks the whole call indefinitely.
    """
    mock_jenkins.query_items.return_value = [_job('wedged')]
    release = threading.Event()
    mock_jenkins.get_build_console_output.side_effect = lambda **kw: release.wait(30) or 'HIT'

    started = time.monotonic()
    try:
        out = search.search_build_logs(mocker.Mock(), pattern='HIT', job_pattern='wedged.*', time_budget_seconds=1)
        elapsed = time.monotonic() - started
    finally:
        release.set()  # let the abandoned fetch finish instead of lingering in the pool

    assert out['summary']['timed_out'] is True
    assert out['summary']['builds_scanned'] == 0
    assert elapsed < 5  # returned on budget instead of waiting out the read


def test_search_time_budget_covers_build_history(mock_jenkins, mocker):
    """The budget must bound every blocking phase, not only the console read."""
    mock_jenkins.query_items.return_value = [_job('slow')]

    def slow_history(*, fullname, count):
        time.sleep(1.2)
        return [Build(number=10, url='u', building=True)]  # nothing to scan, so only history ran

    mock_jenkins.get_build_history.side_effect = slow_history
    mock_jenkins.get_build_console_output.return_value = ''

    out = search.search_build_logs(mocker.Mock(), pattern='x', job_pattern='slow.*', time_budget_seconds=1)

    assert out['summary']['timed_out'] is True


def test_search_does_not_flag_an_outage_when_nothing_was_fetched(mock_jenkins, mocker):
    """A job with only a running build plus one failed history read is not a total outage."""
    mock_jenkins.query_items.return_value = [_job('running'), _job('broken')]

    def history(*, fullname, count):
        if fullname == 'broken':
            msg = 'history unavailable'
            raise RequestsConnectionError(msg)
        return [Build(number=10, url='u', building=True)]

    mock_jenkins.get_build_history.side_effect = history

    out = search.search_build_logs(mocker.Mock(), pattern='x', job_pattern='r.*')

    assert out['summary']['builds_scanned'] == 0
    assert out['summary']['all_fetches_failed'] is False  # no console read was ever attempted
    mock_jenkins.get_build_console_output.assert_not_called()


def test_search_survives_transient_fetch_errors(mock_jenkins, mocker):
    mock_jenkins.query_items.return_value = [_job('a', number=10)]

    def console(**kw: object) -> str:
        if kw['number'] == 9:
            msg = 'master blip'
            raise RequestsConnectionError(msg)
        return 'HIT'

    mock_jenkins.get_build_console_output.side_effect = console

    out = search.search_build_logs(mocker.Mock(), pattern='HIT', job_pattern='a.*', builds_per_job=3)

    assert out['summary']['fetch_errors'] == 1
    assert out['summary']['builds_scanned'] == 2  # a connection blip on one build never aborts the scan
    assert out['summary']['total_matches'] == 2


def test_search_flags_a_total_fetch_outage(mock_jenkins, mocker):
    """Every fetch failing must not look like a clean zero-match search."""
    mock_jenkins.query_items.return_value = [_job('a'), _job('b')]
    msg = 'master down'
    mock_jenkins.get_build_console_output.side_effect = RequestsConnectionError(msg)

    out = search.search_build_logs(mocker.Mock(), pattern='HIT', job_pattern='ab.*')

    assert out['summary']['all_fetches_failed'] is True
    assert out['summary']['fetch_errors'] == 2
    assert out['summary']['builds_scanned'] == 0
    assert out['summary']['jobs_scanned'] == 2  # attempted, so an outage is not reported as "scanned nothing"


def test_search_clamps_time_budget(mock_jenkins, mocker):
    mock_jenkins.query_items.return_value = []

    out = search.search_build_logs(mocker.Mock(), pattern='x', job_pattern='pxc.*', time_budget_seconds=9999)

    assert any('time_budget_seconds adjusted from 9999 to 300' in n for n in out['summary']['notes'])
    assert out['summary']['time_budget_seconds'] == 300
