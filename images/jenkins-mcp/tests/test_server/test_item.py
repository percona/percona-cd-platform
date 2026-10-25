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


@pytest.mark.asyncio
async def test_get_all_items(mock_jenkins, mocker):
    mock_jenkins.get_items.return_value = [
        Job(fullname='job1', color='blue', name='job1', url='1', class_='Job'),
        Folder(fullname='job2', jobs=[], class_='Folder', name='folder', url='1'),
    ]

    result = await item.get_all_items(mocker.Mock())

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

    result = await item.get_all_items(mocker.Mock(), limit=2)

    assert result['total'] == 5
    assert result['returned'] == 2
    assert result['truncated'] is True
    assert len(result['items']) == 2


@pytest.mark.asyncio
async def test_get_item(mock_jenkins, mocker):
    mock_jenkins.get_item.return_value = Job(fullname='job1', color='blue', name='job1', url='1', class_='Job')

    assert await item.get_item(mocker.Mock(), fullname='job1') == {
        'class_': 'Job',
        'color': 'blue',
        'fullname': 'job1',
        'name': 'job1',
        'url': '1',
    }


@pytest.mark.asyncio
async def test_get_item_config(mock_jenkins, mocker):
    mock_jenkins.get_item_config.return_value = '<xml>config</xml>'

    assert await item.get_item_config(mocker.Mock(), fullname='job1') == '<xml>config</xml>'


@pytest.mark.asyncio
async def test_set_item_config(mock_jenkins, mocker):
    mock_jenkins.set_item_config.return_value = None

    await item.set_item_config(mocker.Mock(), fullname='job1', config_xml='<xml>config</xml>')

    mock_jenkins.set_item_config.assert_called_once_with(fullname='job1', config_xml='<xml>config</xml>')


@pytest.mark.asyncio
async def test_create_item(mock_jenkins, mocker):
    mock_jenkins.create_item.return_value = None

    await item.create_item(mocker.Mock(), fullname='folder1/job1', config_xml='<project/>')

    mock_jenkins.create_item.assert_called_once_with(fullname='folder1/job1', config_xml='<project/>')


@pytest.mark.asyncio
async def test_delete_item(mock_jenkins, mocker):
    mock_jenkins.delete_item.return_value = None

    await item.delete_item(mocker.Mock(), fullname='folder1/job1')

    mock_jenkins.delete_item.assert_called_once_with(fullname='folder1/job1')


@pytest.mark.asyncio
async def test_query_items(mock_jenkins, mocker):
    mock_jenkins.query_items.return_value = [
        Job(fullname='job1', color='blue', name='job1', url='1', class_='Job'),
    ]

    result = await item.query_items(
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

    result = await item.query_items(mocker.Mock(), label_pattern='.*aarch64.*')

    assert result['items'][0]['label'] == 'docker-aarch64'
    assert 'assignedLabel' not in result['items'][0]  # surfaced as `label`, raw field not leaked
    assert mock_jenkins.query_items.call_args.kwargs['label_pattern'] == '.*aarch64.*'


@pytest.mark.asyncio
async def test_query_items_include_parameters(mock_jenkins, mocker):
    params = [{'name': 'BRANCH', 'type': 'StringParameterDefinition', 'defaultValue': 'main', 'description': ''}]
    mock_jenkins.query_items.return_value = [
        Job(fullname='job1', color='blue', name='job1', url='1', class_='Job', parameters=params),
    ]

    result = await item.query_items(mocker.Mock(), fullname_pattern='job.*', include_parameters=True)

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

    result = await item.query_items(mocker.Mock(), include_parameters=True, compact_parameters=True)

    assert result['items'] == [
        {
            'fullname': 'job1',
            'parameters': ['BRANCH String=' + 'x' * 200 + '...', 'ARCH Choice=amd64 [amd64|arm64]', 'NOTE String'],
        },
        {'fullname': 'job2', 'parameters': []},
    ]
    assert result['total'] == 2
    assert result['returned'] == 2
    assert result['page'] == 0
    assert result['pages'] == 1
    assert result['truncated'] is False


@pytest.mark.asyncio
async def test_query_items_compact_groups_identical_parameter_lists(mock_jenkins, mocker):
    shared = [{'name': 'BUILD_OS', 'type': 'StringParameterDefinition', 'defaultValue': 'ol9'}]
    mock_jenkins.query_items.return_value = [
        Job(fullname='mb/PR-1', color='blue', name='PR-1', url='1', class_='Job', parameters=shared),
        Job(fullname='solo', color='blue', name='solo', url='2', class_='Job', parameters=[]),
        Job(fullname='mb/PR-2', color='blue', name='PR-2', url='3', class_='Job', parameters=shared),
    ]

    result = await item.query_items(mocker.Mock(), include_parameters=True, compact_parameters=True)

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

    first = await item.query_items(mocker.Mock(), include_parameters=True, compact_parameters=True)
    assert first['pages'] > 1
    assert first['truncated'] is True

    seen = []
    for page in range(first['pages']):
        result = await item.query_items(mocker.Mock(), include_parameters=True, compact_parameters=True, page=page)
        assert len(json.dumps(result['items'], separators=(',', ':'))) <= 300
        assert result['truncated'] is (page < first['pages'] - 1)
        seen += [e['fullname'] for e in result['items']]
    assert seen == [f'job{n}' for n in range(20)]


@pytest.mark.asyncio
async def test_query_items_compact_page_out_of_range(mock_jenkins, mocker):
    mock_jenkins.query_items.return_value = [Job(fullname='job1', color='blue', name='job1', url='1', class_='Job')]

    with pytest.raises(ValueError, match='page 1 is out of range, this query has 1 page'):
        await item.query_items(mocker.Mock(), include_parameters=True, compact_parameters=True, page=1)


@pytest.mark.asyncio
async def test_query_items_compact_needs_include_parameters(mock_jenkins, mocker):
    mock_jenkins.query_items.return_value = [
        Job(fullname='job1', color='blue', name='job1', url='1', class_='Job'),
    ]

    result = await item.query_items(mocker.Mock(), compact_parameters=True)

    # compact without include_parameters keeps the normal compact item list
    assert result['items'] == [{'class_': 'Job', 'color': 'blue', 'fullname': 'job1', 'name': 'job1', 'url': '1'}]


@pytest.mark.asyncio
async def test_build_item(mock_jenkins, mocker):
    mock_jenkins.build_item.return_value = None

    await item.build_item(
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

    result = await item.get_item_parameters(mocker.Mock(), fullname='job1')

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
async def test_query_items_include_builds(mock_jenkins, mocker):
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

    result = await item.query_items(mocker.Mock(), include_builds=10)

    mock_jenkins.query_items.assert_called_once()
    assert mock_jenkins.query_items.call_args.kwargs['include_builds'] == 10
    assert result['items'] == [
        {
            'fullname': 'job1',
            'builds': [
                '#2 UNSTABLE 2026-10-20T12:40Z 2h37m user Jane Doe',
                '#1 SUCCESS 2026-10-19T08:53Z 1h00m upstream parent/ARCH=x86_64 #73',
            ],
        }
    ]
    assert (result['builds_total'], result['returned'], result['total'], result['pages']) == (2, 1, 2, 1)


@pytest.mark.asyncio
async def test_query_items_builds_since_days_and_more(mock_jenkins, mocker):
    mock_jenkins.query_items.return_value = [
        Job(fullname='busy', color='blue', name='busy', url='1', class_='Job', builds=[_build(2), _build(1)]),
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

    result = await item.query_items(mocker.Mock(), include_builds=2, builds_since_days=30)

    # busy filled its 2-build cap inside the window, so older builds may exist: more=True
    assert [(e['fullname'], len(e['builds']), e.get('more', False)) for e in result['items']] == [
        ('busy', 2, True),
        ('old', 1, False),
        ('never', 0, False),
    ]
    assert result['builds_total'] == 3


@pytest.mark.asyncio
async def test_query_items_splits_a_job_larger_than_a_page(mock_jenkins, mocker):
    mocker.patch('mcp_jenkins.server.item._COMPACT_PAGE_CHARS', 400)
    builds = [_build(n, causes=('Started by user ' + 'x' * 40,)) for n in range(30, 0, -1)]
    mock_jenkins.query_items.return_value = [
        Job(fullname='big', color='blue', name='big', url='1', class_='Job', builds=builds),
    ]

    first = await item.query_items(mocker.Mock(), include_builds=30)
    parts, lines = [], []
    for page in range(first['pages']):
        result = await item.query_items(mocker.Mock(), include_builds=30, page=page)
        assert len(json.dumps(result['items'], separators=(',', ':'))) <= 400
        parts += [e['part'] for e in result['items']]
        lines += [line for e in result['items'] for line in e['builds']]

    assert parts == list(range(1, len(parts) + 1))
    assert len(parts) > 1
    assert [line.split()[0] for line in lines] == [f'#{n}' for n in range(30, 0, -1)]
    assert first['returned'] == 1


@pytest.mark.asyncio
async def test_query_items_builds_and_parameters_are_exclusive(mock_jenkins, mocker):
    with pytest.raises(ValueError, match='include_builds or include_parameters, not both'):
        await item.query_items(mocker.Mock(), include_builds=5, include_parameters=True)
