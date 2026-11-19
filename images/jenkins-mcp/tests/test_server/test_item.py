import json
import time

import pytest

from mcp_jenkins.jenkins.model.item import Folder, Job
from mcp_jenkins.server import item


@pytest.fixture
def mock_jenkins(mocker):
    mock_jenkins = mocker.Mock()

    mocker.patch('mcp_jenkins.server.item.jenkins', return_value=mock_jenkins)

    yield mock_jenkins


@pytest.fixture
def bulk_builds(mocker):
    """Send every include_builds call down the one-request path, whatever the job count, uncapped."""
    mocker.patch('mcp_jenkins.server.item._PER_JOB_BUILDS_MAX', 0)
    mocker.patch('mcp_jenkins.server.item._BULK_BUILDS_MAX', 200)


@pytest.mark.asyncio
async def test_get_all_items(mock_jenkins, mocker):
    mock_jenkins.get_items.return_value = [
        Job(fullname='job1', color='blue', name='job1', url='1', class_='Job'),
        Folder(fullname='job2', jobs=[], class_='Folder', name='folder', url='1'),
    ]

    result = item.get_all_items(mocker.Mock())

    assert result['total'] == 2
    assert result['truncated'] is False
    assert result['items'] == [
        {'class_': 'Job', 'color': 'blue', 'fullname': 'job1', 'name': 'job1', 'url': '1'},
        {'class_': 'Folder', 'fullname': 'job2', 'name': 'folder', 'url': '1'},
    ]


@pytest.mark.asyncio
async def test_get_all_items_truncates(mock_jenkins, mocker):
    mock_jenkins.get_items.return_value = [
        Job(fullname=f'job{i}', color='blue', name=f'job{i}', url='1', class_='Job') for i in range(5)
    ]

    result = item.get_all_items(mocker.Mock(), limit=2)

    assert result['total'] == 5
    assert result['returned'] == 2
    assert result['truncated'] is True
    assert len(result['items']) == 2


@pytest.mark.asyncio
async def test_get_item(mock_jenkins, mocker):
    mock_jenkins.get_item.return_value = Job(fullname='job1', color='blue', name='job1', url='1', class_='Job')

    assert item.get_item(mocker.Mock(), fullname='job1') == {
        'class_': 'Job',
        'color': 'blue',
        'fullname': 'job1',
        'name': 'job1',
        'url': '1',
    }


@pytest.mark.asyncio
async def test_get_item_config(mock_jenkins, mocker):
    mock_jenkins.get_item_config.return_value = '<xml>config</xml>'

    assert item.get_item_config(mocker.Mock(), fullname='job1') == '<xml>config</xml>'


@pytest.mark.asyncio
async def test_set_item_config(mock_jenkins, mocker):
    mock_jenkins.set_item_config.return_value = None

    item.set_item_config(mocker.Mock(), fullname='job1', config_xml='<xml>config</xml>')

    mock_jenkins.set_item_config.assert_called_once_with(fullname='job1', config_xml='<xml>config</xml>')


@pytest.mark.asyncio
async def test_create_item(mock_jenkins, mocker):
    mock_jenkins.create_item.return_value = None

    item.create_item(mocker.Mock(), fullname='folder1/job1', config_xml='<project/>')

    mock_jenkins.create_item.assert_called_once_with(fullname='folder1/job1', config_xml='<project/>')


@pytest.mark.asyncio
async def test_delete_item(mock_jenkins, mocker):
    mock_jenkins.delete_item.return_value = None

    item.delete_item(mocker.Mock(), fullname='folder1/job1')

    mock_jenkins.delete_item.assert_called_once_with(fullname='folder1/job1')


@pytest.mark.asyncio
async def test_query_items(mock_jenkins, mocker):
    mock_jenkins.query_items.return_value = [
        Job(fullname='job1', color='blue', name='job1', url='1', class_='Job'),
    ]

    result = item.query_items(
        mocker.Mock(),
        class_pattern='.*',
        fullname_pattern='job.*',
        color_pattern='blue',
    )

    assert result['total'] == 1
    assert result['items'] == [
        {'class_': 'Job', 'color': 'blue', 'fullname': 'job1', 'name': 'job1', 'url': '1'},
    ]


@pytest.mark.asyncio
async def test_query_items_surfaces_label_and_passes_filter(mock_jenkins, mocker):
    from mcp_jenkins.jenkins.model.item import AssignedLabel

    mock_jenkins.query_items.return_value = [
        Job(
            fullname='fs',
            color='blue',
            name='fs',
            url='1',
            class_='FreeStyleProject',
            assignedLabel=AssignedLabel(name='docker-aarch64'),
        ),
    ]

    result = item.query_items(mocker.Mock(), label_pattern='.*aarch64.*')

    assert result['items'][0]['label'] == 'docker-aarch64'
    assert 'assignedLabel' not in result['items'][0]  # surfaced as `label`, raw field not leaked
    assert mock_jenkins.query_items.call_args.kwargs['label_pattern'] == '.*aarch64.*'


@pytest.mark.asyncio
async def test_query_items_include_parameters(mock_jenkins, mocker):
    params = [{'name': 'BRANCH', 'type': 'StringParameterDefinition', 'defaultValue': 'main', 'description': ''}]
    mock_jenkins.query_items.return_value = [
        Job(fullname='job1', color='blue', name='job1', url='1', class_='Job', parameters=params),
    ]

    result = item.query_items(mocker.Mock(), fullname_pattern='job.*', include_parameters=True)

    assert result['items'][0]['parameters'] == params
    assert mock_jenkins.query_items.call_args.kwargs['include_parameters'] is True


@pytest.mark.asyncio
async def test_query_items_compact_parameters(mock_jenkins, mocker):
    params = [
        {'name': 'BRANCH', 'type': 'StringParameterDefinition', 'defaultValue': 'x' * 300, 'description': 'long'},
        {
            'name': 'ARCH',
            'type': 'ChoiceParameterDefinition',
            'defaultValue': 'amd64',
            'description': 'Arch',
            'choices': ['amd64', 'arm64'],
        },
        {'name': 'NOTE', 'type': 'StringParameterDefinition', 'defaultValue': '', 'description': ''},
    ]
    mock_jenkins.query_items.return_value = [
        Job(fullname='job1', color='blue', name='job1', url='1', class_='Job', parameters=params),
        Job(fullname='job2', color='blue', name='job2', url='2', class_='Job', parameters=[]),
    ]

    result = item.query_items(mocker.Mock(), include_parameters=True, compact_parameters=True)

    assert result['items'] == [
        {
            'fullname': 'job1',
            'parameters': ['BRANCH String=' + 'x' * 200 + '...', 'ARCH Choice=amd64 [amd64|arm64]', 'NOTE String'],
        },
        {'fullname': 'job2', 'parameters': []},
    ]
    assert result['total'] == 2
    assert result['returned'] == 2
    assert result['next_cursor'] is None
    assert result['truncated'] is False


@pytest.mark.asyncio
async def test_query_items_compact_groups_identical_parameter_lists(mock_jenkins, mocker):
    shared = [{'name': 'BUILD_OS', 'type': 'StringParameterDefinition', 'defaultValue': 'ol9'}]
    mock_jenkins.query_items.return_value = [
        Job(fullname='mb/PR-1', color='blue', name='PR-1', url='1', class_='Job', parameters=shared),
        Job(fullname='solo', color='blue', name='solo', url='2', class_='Job', parameters=[]),
        Job(fullname='mb/PR-2', color='blue', name='PR-2', url='3', class_='Job', parameters=shared),
    ]

    result = item.query_items(mocker.Mock(), include_parameters=True, compact_parameters=True)

    assert result['items'] == [
        {'jobs': ['mb/PR-1', 'mb/PR-2'], 'parameters': ['BUILD_OS String=ol9']},
        {'fullname': 'solo', 'parameters': []},
    ]
    assert result['returned'] == 3


@pytest.mark.asyncio
async def test_query_items_compact_pages_cover_every_job(mock_jenkins, mocker):
    mocker.patch('mcp_jenkins.server.item._COMPACT_PAGE_CHARS', 300)
    mock_jenkins.query_items.return_value = [
        Job(
            fullname=f'job{n}',
            color='blue',
            name=f'job{n}',
            url=str(n),
            class_='Job',
            parameters=[{'name': f'P{n}', 'type': 'StringParameterDefinition', 'defaultValue': 'v' * 50}],
        )
        for n in range(20)
    ]

    seen, cursor, calls = [], None, 0
    while True:
        result = item.query_items(mocker.Mock(), include_parameters=True, compact_parameters=True, cursor=cursor)
        calls += 1
        assert len(json.dumps(result['items'], separators=(',', ':'))) <= 300
        assert result['truncated'] is (result['next_cursor'] is not None)
        seen += [e['fullname'] for e in result['items']]
        cursor = result['next_cursor']
        if cursor is None:
            break
    assert calls > 1
    assert seen == sorted(f'job{n}' for n in range(20))


@pytest.mark.asyncio
async def test_query_items_compact_page_out_of_range(mock_jenkins, mocker):
    mock_jenkins.query_items.return_value = [Job(fullname='job1', color='blue', name='job1', url='1', class_='Job')]

    with pytest.raises(ValueError, match='is not one this tool returned'):
        item.query_items(mocker.Mock(), include_parameters=True, compact_parameters=True, cursor='job1')


@pytest.mark.asyncio
async def test_query_items_compact_needs_include_parameters(mock_jenkins, mocker):
    mock_jenkins.query_items.return_value = [
        Job(fullname='job1', color='blue', name='job1', url='1', class_='Job'),
    ]

    result = item.query_items(mocker.Mock(), compact_parameters=True)

    # compact without include_parameters keeps the normal compact item list
    assert result['items'] == [{'class_': 'Job', 'color': 'blue', 'fullname': 'job1', 'name': 'job1', 'url': '1'}]


@pytest.mark.asyncio
async def test_build_item(mock_jenkins, mocker):
    mock_jenkins.build_item.return_value = None

    item.build_item(
        mocker.Mock(),
        fullname='job1',
        data={'param1': 'value1'},
        build_type='buildWithParameters',
    )

    mock_jenkins.build_item.assert_called_once_with(
        fullname='job1', data={'param1': 'value1'}, build_type='buildWithParameters'
    )


@pytest.mark.asyncio
async def test_get_item_parameters(mock_jenkins, mocker):
    mock_jenkins.get_item_parameters.return_value = [
        {'name': 'BRANCH', 'type': 'StringParameterDefinition', 'defaultValue': 'main', 'description': 'Branch'},
    ]

    result = item.get_item_parameters(mocker.Mock(), fullname='job1')

    assert result == [
        {'name': 'BRANCH', 'type': 'StringParameterDefinition', 'defaultValue': 'main', 'description': 'Branch'},
    ]
    mock_jenkins.get_item_parameters.assert_called_once_with(fullname='job1')


def test_assigned_label_name_coerces_non_string():
    from mcp_jenkins.jenkins.model.item import UnknownItem

    # A matrix/maven job routes through UnknownItem with assignedLabel as a raw dict; a non-string
    # name must coerce to None so it can never reach re.search() in query_items.
    bad = UnknownItem(_class='hudson.matrix.MatrixProject', name='m', url='u', assignedLabel={'name': 123})
    assert bad.assigned_label_name() is None
    ok = UnknownItem(_class='hudson.matrix.MatrixProject', name='m', url='u', assignedLabel={'name': 'docker-aarch64'})
    assert ok.assigned_label_name() == 'docker-aarch64'


def _build(number, result='SUCCESS', *, hours_ago=1, duration=3_600_000, causes=('Started by timer',)):
    return {
        'number': number,
        'result': result,
        'building': False,
        'timestamp': int((time.time() - hours_ago * 3600) * 1000),
        'duration': duration,
        'causes': list(causes),
    }


@pytest.mark.asyncio
async def test_query_items_include_builds(mock_jenkins, bulk_builds, mocker):
    upstream = 'Started by upstream project "parent/ARCH=x86_64" build number 73'
    mock_jenkins.query_items.return_value = [
        Job(
            fullname='job1',
            color='blue',
            name='job1',
            url='1',
            class_='Job',
            builds=[
                {
                    **_build(2, 'UNSTABLE', duration=9_420_000, causes=('Started by user Jane Doe',)),
                    'timestamp': 1792500000000,
                },
                {**_build(1, causes=(upstream,)), 'timestamp': 1792400000000},
            ],
        ),
        Folder(fullname='dir', jobs=[], class_='Folder', name='dir', url='2'),
    ]

    result = item.query_items(mocker.Mock(), include_builds=10, fullname_pattern='job1|dir')

    # one build past the cap is fetched, so more=True only when an older build really exists
    assert mock_jenkins.query_items.call_args.kwargs['include_builds'] == 11
    assert result['items'] == [
        {
            'fullname': 'job1',
            'builds': [
                '#2 UNSTABLE 2026-10-20T12:40Z 2h37m user Jane Doe',
                '#1 SUCCESS 2026-10-19T08:53Z 1h00m upstream parent/ARCH=x86_64 #73',
            ],
        }
    ]
    assert (result['builds_total'], result['returned'], result['total'], result['next_cursor']) == (2, 1, 2, None)


@pytest.mark.asyncio
async def test_query_items_builds_since_days_and_more(mock_jenkins, bulk_builds, mocker):
    mock_jenkins.query_items.return_value = [
        Job(
            fullname='busy', color='blue', name='busy', url='1', class_='Job', builds=[_build(3), _build(2), _build(1)]
        ),
        Job(
            fullname='old',
            color='blue',
            name='old',
            url='2',
            class_='Job',
            builds=[_build(5), _build(4, hours_ago=24 * 40)],
        ),
        Job(fullname='never', color='notbuilt', name='never', url='3', class_='Job', builds=[]),
    ]

    result = item.query_items(
        mocker.Mock(), include_builds=2, builds_since_days=30, fullname_pattern='^(busy|old|never)$'
    )

    # busy has a third build inside the window past its 2-build cap: more=True
    assert [(e['fullname'], len(e['builds']), e.get('more', False)) for e in result['items']] == [
        ('busy', 2, True),
        ('never', 0, False),
        ('old', 1, False),
    ]
    assert result['builds_total'] == 3


@pytest.mark.asyncio
async def test_query_items_splits_a_job_larger_than_a_page(mock_jenkins, bulk_builds, mocker):
    mocker.patch('mcp_jenkins.server.item._COMPACT_PAGE_CHARS', 400)
    builds = [_build(n, causes=('Started by user ' + 'x' * 40,)) for n in range(30, 0, -1)]
    mock_jenkins.query_items.return_value = [
        Job(fullname='big', color='blue', name='big', url='1', class_='Job', builds=builds),
    ]

    parts, lines, cursor, first = [], [], None, None
    while True:
        result = item.query_items(mocker.Mock(), include_builds=30, fullname_pattern='^big$', cursor=cursor)
        first = first or result
        assert len(json.dumps(result['items'], separators=(',', ':'))) <= 400
        parts += [e['part'] for e in result['items']]
        lines += [line for e in result['items'] for line in e['builds']]
        cursor = result['next_cursor']
        if cursor is None:
            break

    assert parts == list(range(1, len(parts) + 1))
    assert len(parts) > 1
    assert [line.split()[0] for line in lines] == [f'#{n}' for n in range(30, 0, -1)]
    assert first['returned'] == 1


@pytest.mark.asyncio
async def test_query_items_builds_and_parameters_are_exclusive(mock_jenkins, mocker):
    with pytest.raises(ValueError, match='include_builds or include_parameters, not both'):
        item.query_items(mocker.Mock(), include_builds=5, include_parameters=True)


def test_query_items_and_get_all_items_run_off_the_event_loop():
    import inspect

    # FastMCP runs a sync tool in a worker thread, a slow Jenkins read must not stall /healthz
    assert not inspect.iscoroutinefunction(item.query_items)
    assert not inspect.iscoroutinefunction(item.get_all_items)


def test_query_items_compact_splits_an_oversized_group(mock_jenkins, mocker):
    mock_jenkins.query_items.return_value = [
        Job(fullname=f'mb/feature-branch-{n:04d}', color='blue', name='x', url='u', class_='Job', parameters=[])
        for n in range(2000)
    ]

    seen, cursor = [], None
    while True:
        result = item.query_items(
            mocker.Mock(), include_parameters=True, compact_parameters=True, limit=2000, cursor=cursor
        )
        assert len(json.dumps(result['items'], separators=(',', ':'))) <= item._COMPACT_PAGE_CHARS
        seen += [name for e in result['items'] for name in e['jobs']]
        cursor = result['next_cursor']
        if cursor is None:
            break

    assert seen == [f'mb/feature-branch-{n:04d}' for n in range(2000)]


def test_query_items_compact_leaves_folders_out(mock_jenkins, mocker):
    mock_jenkins.query_items.return_value = [
        Folder(fullname='dir', jobs=[], class_='Folder', name='dir', url='1'),
        Job(fullname='job', color='blue', name='job', url='2', class_='Job', parameters=[]),
    ]

    result = item.query_items(mocker.Mock(), include_parameters=True, compact_parameters=True)

    assert result['items'] == [{'fullname': 'job', 'parameters': []}]


def test_query_items_cursor_never_repeats_a_job_when_entries_grow(mock_jenkins, bulk_builds, mocker):
    mocker.patch('mcp_jenkins.server.item._COMPACT_PAGE_CHARS', 500)

    def jobs(extra):
        return [
            Job(
                fullname=f'job{n:02d}',
                color='blue',
                name='x',
                url='u',
                class_='Job',
                builds=[_build(k) for k in range(2 + (extra if n == 0 else 0))],
            )
            for n in range(12)
        ]

    mock_jenkins.query_items.return_value = jobs(0)
    first = item.query_items(mocker.Mock(), include_builds=20, fullname_pattern='^job')
    mock_jenkins.query_items.return_value = jobs(8)  # job00 gains builds between the two calls
    second = item.query_items(mocker.Mock(), include_builds=20, fullname_pattern='^job', cursor=first['next_cursor'])

    names = [e['fullname'] for e in first['items'] + second['items']]
    assert len(names) == len(set(names))
    assert names == sorted(names)
    assert names[: len(first['items']) + 1][-1] == f'job{len(first["items"]):02d}'


def test_query_items_rejects_a_non_positive_window(mock_jenkins, mocker):
    with pytest.raises(ValueError, match='builds_since_days must be 1 or more'):
        item.query_items(mocker.Mock(), include_builds=5, builds_since_days=-7)
    mock_jenkins.query_items.assert_not_called()


def test_query_items_more_is_false_at_exactly_the_cap(mock_jenkins, bulk_builds, mocker):
    mock_jenkins.query_items.return_value = [
        Job(
            fullname='exact',
            color='blue',
            name='exact',
            url='1',
            class_='Job',
            builds=[_build(k) for k in range(5, 0, -1)],
        ),
    ]

    result = item.query_items(mocker.Mock(), include_builds=5, fullname_pattern='^exact$')

    assert len(result['items'][0]['builds']) == 5
    assert 'more' not in result['items'][0]


def test_query_items_narrow_pattern_reads_builds_job_by_job(mock_jenkins, mocker):
    mock_jenkins.query_items.return_value = [
        Job(fullname='pmm3-a', color='blue', name='a', url='1', class_='Job'),
        Folder(fullname='pmm3-dir', jobs=[], class_='Folder', name='d', url='2'),
    ]
    mock_jenkins.get_job_builds.return_value = [_build(1)]

    result = item.query_items(mocker.Mock(), fullname_pattern='^pmm3-', include_builds=50)

    # the whole-tree allBuilds projection is never requested for a narrow pattern
    assert all(c.kwargs.get('include_builds', 0) == 0 for c in mock_jenkins.query_items.call_args_list)
    mock_jenkins.get_job_builds.assert_called_once_with(fullname='pmm3-a', count=51)
    assert [e['fullname'] for e in result['items']] == ['pmm3-a']


def test_query_items_wide_pattern_falls_back_to_one_tree_request(mock_jenkins, mocker):
    mock_jenkins.query_items.return_value = [
        Job(fullname=f'pmm3-{n}', color='blue', name='x', url='u', class_='Job', builds=[]) for n in range(25)
    ]

    item.query_items(mocker.Mock(), fullname_pattern='^pmm3-', include_builds=10)

    mock_jenkins.get_job_builds.assert_not_called()
    assert mock_jenkins.query_items.call_args_list[-1].kwargs['include_builds'] == 11


_MATCH_EVERYTHING = ['', '.*', '^.*$', '.+', '.', '(?s).*', 'a|.*', 'x?', '[\\s\\S]', '^']


def test_query_items_builds_without_a_pattern_is_refused(mock_jenkins, mocker):
    with pytest.raises(ValueError, match='include_builds needs a pattern that narrows the jobs') as refused:
        item.query_items(mocker.Mock(), include_builds=200)

    for name in ('fullname_pattern', 'label_pattern', 'color_pattern', 'class_pattern'):
        assert name in str(refused.value)
    assert mock_jenkins.method_calls == []


def test_query_items_builds_since_days_alone_is_refused(mock_jenkins, mocker):
    # builds_since_days filters in the gateway, the master still loads every job's history
    with pytest.raises(ValueError, match='include_builds needs a pattern that narrows the jobs'):
        item.query_items(mocker.Mock(), include_builds=200, builds_since_days=1)

    assert mock_jenkins.method_calls == []


@pytest.mark.parametrize('field', ['fullname_pattern', 'label_pattern', 'color_pattern', 'class_pattern'])
@pytest.mark.parametrize('pattern', _MATCH_EVERYTHING)
def test_query_items_builds_with_a_match_everything_pattern_is_refused(mock_jenkins, mocker, field, pattern):
    with pytest.raises(ValueError, match='include_builds needs a pattern that narrows the jobs'):
        item.query_items(mocker.Mock(), include_builds=10, **{field: pattern})

    assert mock_jenkins.method_calls == []


@pytest.mark.parametrize(
    ('field', 'pattern'),
    [
        ('fullname_pattern', '^pmm3-'),
        ('label_pattern', 'docker-aarch64'),
        ('color_pattern', 'red'),
        ('class_pattern', 'FreeStyleProject'),
    ],
)
def test_query_items_builds_with_each_narrowing_pattern_is_accepted(mock_jenkins, mocker, field, pattern):
    mock_jenkins.query_items.return_value = [Job(fullname='pmm3-a', color='red', name='a', url='1', class_='Job')]
    mock_jenkins.get_job_builds.return_value = [_build(1)]

    result = item.query_items(mocker.Mock(), include_builds=5, **{field: pattern})

    # the narrow selection is read without builds first, then each job's history alone
    assert mock_jenkins.query_items.call_args_list[0].kwargs[field] == pattern
    assert all(c.kwargs.get('include_builds', 0) == 0 for c in mock_jenkins.query_items.call_args_list)
    mock_jenkins.get_job_builds.assert_called_once_with(fullname='pmm3-a', count=6)
    assert [e['fullname'] for e in result['items']] == ['pmm3-a']


def test_query_items_one_narrowing_pattern_is_enough(mock_jenkins, mocker):
    mock_jenkins.query_items.return_value = []

    item.query_items(mocker.Mock(), include_builds=5, fullname_pattern='.*', label_pattern='docker-x64')

    mock_jenkins.query_items.assert_called_once()


def test_query_items_without_builds_needs_no_pattern(mock_jenkins, mocker):
    mock_jenkins.query_items.return_value = []

    item.query_items(mocker.Mock())
    item.query_items(mocker.Mock(), include_parameters=True, compact_parameters=True)

    assert mock_jenkins.query_items.call_count == 2


def test_query_items_include_builds_is_capped_at_200(mock_jenkins, bulk_builds, mocker):
    mock_jenkins.query_items.return_value = [Job(fullname='a', color='blue', name='a', url='1', class_='Job')]

    item.query_items(mocker.Mock(), include_builds=10_000, fullname_pattern='^a')

    assert mock_jenkins.query_items.call_args_list[-1].kwargs['include_builds'] == 201


def test_query_items_refused_call_sends_no_request_to_the_master(mocker):
    from mcp_jenkins.jenkins import Jenkins

    session = mocker.Mock()
    mocker.patch('mcp_jenkins.jenkins.rest_client.requests.Session', autospec=True, return_value=session)
    client = Jenkins(url='https://example.com/', username='u', password='p')
    mocker.patch('mcp_jenkins.server.item.jenkins', return_value=client)

    with pytest.raises(ValueError, match='include_builds needs a pattern'):
        item.query_items(mocker.Mock(), include_builds=200, fullname_pattern='.*', builds_since_days=90)

    session.request.assert_not_called()
    session.get.assert_not_called()


def _jobs(count, builds=None):
    return [
        Job(fullname=f'pmm3-{n:02d}', color='blue', name='x', url='u', class_='Job', builds=builds)
        for n in range(count)
    ]


def test_query_items_wide_selection_caps_bulk_builds_at_20(mock_jenkins, mocker):
    mock_jenkins.query_items.return_value = _jobs(21, builds=[_build(k) for k in range(21, 0, -1)])

    result = item.query_items(mocker.Mock(), fullname_pattern='^pmm3-', include_builds=200)

    # the one-request read asks Jenkins for 21 builds per job, not 201
    assert mock_jenkins.query_items.call_args_list[-1].kwargs['include_builds'] == 21
    mock_jenkins.get_job_builds.assert_not_called()
    assert result['builds_cap'] == 20
    assert '21 jobs' in result['note']
    assert all(len(e['builds']) == 20 and e['more'] is True for e in result['items'])


def test_query_items_wide_selection_cap_holds_on_every_page(mock_jenkins, mocker):
    mocker.patch('mcp_jenkins.server.item._COMPACT_PAGE_CHARS', 2000)
    mock_jenkins.query_items.return_value = _jobs(25, builds=[_build(k) for k in range(21, 0, -1)])

    cursor, pages = None, 0
    while True:
        result = item.query_items(mocker.Mock(), fullname_pattern='^pmm3-', include_builds=200, cursor=cursor)
        pages += 1
        assert result['builds_cap'] == 20
        assert mock_jenkins.query_items.call_args_list[-1].kwargs['include_builds'] == 21
        cursor = result['next_cursor']
        if cursor is None:
            break
    assert pages > 1


def test_query_items_narrow_selection_keeps_the_200_cap(mock_jenkins, mocker):
    mock_jenkins.query_items.return_value = _jobs(20)
    mock_jenkins.get_job_builds.return_value = [_build(1)]

    result = item.query_items(mocker.Mock(), fullname_pattern='^pmm3-', include_builds=200)

    assert mock_jenkins.get_job_builds.call_count == 20
    assert all(c.kwargs['count'] == 201 for c in mock_jenkins.get_job_builds.call_args_list)
    assert 'builds_cap' not in result
    assert 'note' not in result


def test_query_items_wide_selection_under_the_cap_is_unchanged(mock_jenkins, mocker):
    mock_jenkins.query_items.return_value = _jobs(21, builds=[_build(1)])

    result = item.query_items(mocker.Mock(), fullname_pattern='^pmm3-', include_builds=10)

    assert mock_jenkins.query_items.call_args_list[-1].kwargs['include_builds'] == 11
    assert 'builds_cap' not in result
    assert 'note' not in result
