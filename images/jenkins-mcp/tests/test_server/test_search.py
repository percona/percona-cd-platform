import pytest
from requests.exceptions import HTTPError

from mcp_jenkins.jenkins.model.build import Build
from mcp_jenkins.jenkins.model.item import Folder, Job
from mcp_jenkins.server import search


@pytest.fixture
def mock_jenkins(mocker):
    mock_jenkins = mocker.Mock()
    mocker.patch('mcp_jenkins.server.search.jenkins', return_value=mock_jenkins)
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


@pytest.mark.asyncio
async def test_search_happy_path(mock_jenkins, mocker):
    mock_jenkins.query_items.return_value = [_job('pxc-a'), _job('pxc-b')]
    mock_jenkins.get_build_console_output.side_effect = lambda **kw: (
        'ERROR x\nERROR y' if kw['fullname'] == 'pxc-a' else ''
    )

    out = await search.search_build_logs(mocker.Mock(), pattern='ERROR', job_pattern='pxc.*')

    assert out['summary']['jobs_scanned'] == 2
    assert out['summary']['jobs_with_matches'] == 1
    assert out['summary']['total_matches'] == 2
    assert out['results'][0] == {'job_fullname': 'pxc-a', 'build_number': 10, 'matching_lines': ['ERROR x', 'ERROR y']}
    mock_jenkins.query_items.assert_called_once_with(fullname_pattern='pxc.*', folder_depth=None)


@pytest.mark.asyncio
@pytest.mark.parametrize('bad', ['', '.*', '*', '   '])
async def test_search_rejects_catch_all(mock_jenkins, mocker, bad):
    with pytest.raises(ValueError, match='job_pattern'):
        await search.search_build_logs(mocker.Mock(), pattern='x', job_pattern=bad)
    mock_jenkins.query_items.assert_not_called()


@pytest.mark.asyncio
async def test_search_max_jobs_cap(mock_jenkins, mocker):
    mock_jenkins.query_items.return_value = [_job(f'pxc-{i}') for i in range(30)]
    mock_jenkins.get_build_console_output.return_value = ''

    out = await search.search_build_logs(mocker.Mock(), pattern='x', job_pattern='pxc.*', max_jobs=5)

    assert out['summary']['jobs_scanned'] == 5
    assert out['summary']['truncated_jobs'] is True
    assert mock_jenkins.get_build_console_output.call_count == 5


@pytest.mark.asyncio
async def test_search_clamps_and_notes(mock_jenkins, mocker):
    mock_jenkins.query_items.return_value = []

    out = await search.search_build_logs(mocker.Mock(), pattern='x', job_pattern='pxc.*', max_jobs=9999)

    assert any('max_jobs adjusted from 9999 to 200' in n for n in out['summary']['notes'])


@pytest.mark.asyncio
async def test_search_max_total_matches_early_stop(mock_jenkins, mocker):
    mock_jenkins.query_items.return_value = [_job('a'), _job('b'), _job('c')]
    mock_jenkins.get_build_console_output.side_effect = lambda **kw: '\n'.join(f'ERROR {i}' for i in range(kw['limit']))

    out = await search.search_build_logs(mocker.Mock(), pattern='ERROR', job_pattern='job.*', max_total_matches=30)

    assert out['summary']['total_matches'] == 30
    assert out['summary']['truncated_matches'] is True
    assert out['summary']['jobs_scanned'] == 2


@pytest.mark.asyncio
async def test_search_ignore_case_prefixes_pattern(mock_jenkins, mocker):
    mock_jenkins.query_items.return_value = [_job('a')]
    mock_jenkins.get_build_console_output.return_value = 'error'

    await search.search_build_logs(mocker.Mock(), pattern='ERROR', job_pattern='a.*', ignore_case=True)

    assert mock_jenkins.get_build_console_output.call_args.kwargs['pattern'] == '(?i)ERROR'


@pytest.mark.asyncio
async def test_search_skips_folders_and_unbuilt(mock_jenkins, mocker):
    folder = Folder(fullname='dir', name='dir', url='u', class_='Folder', jobs=[])
    unbuilt = Job(fullname='new', name='new', url='u', class_='Job', color='notbuilt')
    mock_jenkins.query_items.return_value = [folder, unbuilt, _job('built')]
    mock_jenkins.get_build_console_output.return_value = 'MATCH'

    out = await search.search_build_logs(mocker.Mock(), pattern='MATCH', job_pattern='b.*')

    assert out['summary']['jobs_matched_pattern'] == 1
    assert out['summary']['jobs_scanned'] == 1
    assert mock_jenkins.get_build_console_output.call_count == 1


@pytest.mark.asyncio
async def test_search_builds_per_job_skips_rotated(mock_jenkins, mocker):
    mock_jenkins.query_items.return_value = [_job('a', number=10)]
    calls = []

    def console(**kw: object) -> str:
        calls.append(kw['number'])
        if kw['number'] == 9:
            raise HTTPError(response=mocker.Mock(status_code=404))
        return 'HIT'

    mock_jenkins.get_build_console_output.side_effect = console

    out = await search.search_build_logs(mocker.Mock(), pattern='HIT', job_pattern='a.*', builds_per_job=3)

    assert calls == [10, 9, 8]
    assert out['summary']['builds_scanned'] == 2
    assert out['summary']['total_matches'] == 2
