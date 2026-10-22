import pytest

from mcp_jenkins.core.fleet import Fleet, Master
from mcp_jenkins.server import fleet


@pytest.fixture
def two_masters(mocker):
    """A fleet of ps80 + ps57 with client_for returning per-master mocks."""
    cfg = Fleet(
        masters=[
            Master(name='ps80', url='https://ps80.cd', username='svc', token='x'),  # noqa: S106
            Master(name='ps57', url='https://ps57.cd', username='svc', token='x'),  # noqa: S106
        ]
    )
    mocker.patch('mcp_jenkins.server.fleet.get_fleet', return_value=cfg)

    clients = {'ps80': mocker.Mock(), 'ps57': mocker.Mock()}
    mocker.patch('mcp_jenkins.server.fleet.client_for', side_effect=lambda name: clients[name])
    return clients


@pytest.mark.asyncio
async def test_list_masters_reports_liveness(two_masters):
    two_masters['ps80'].get_version.return_value = '2.541.3'
    two_masters['ps57'].get_version.side_effect = RuntimeError('connection refused')

    result = await fleet.list_masters()

    by_name = {m['name']: m for m in result}
    assert by_name['ps80'] == {'name': 'ps80', 'url': 'https://ps80.cd', 'reachable': True, 'version': '2.541.3'}
    assert by_name['ps57']['reachable'] is False
    assert 'connection refused' in by_name['ps57']['error']


def _job(fullname: str) -> object:
    from mcp_jenkins.jenkins.model.item import serialize_item

    return serialize_item(
        {
            '_class': 'org.jenkinsci.plugins.workflow.job.WorkflowJob',
            'name': fullname,
            'fullName': fullname,
            'url': f'u/{fullname}',
            'color': 'blue',
        }
    )


@pytest.mark.asyncio
async def test_find_items_searches_every_master_and_tags_results(two_masters):
    two_masters['ps80'].query_items.return_value = []
    two_masters['ps57'].query_items.return_value = [_job('ec2-instance-checks')]

    result = await fleet.find_items(fullname_pattern='ec2-instance')

    assert result['items'] == [
        {
            'master': 'ps57',
            'class_': 'org.jenkinsci.plugins.workflow.job.WorkflowJob',
            'url': 'u/ec2-instance-checks',
            'fullname': 'ec2-instance-checks',
            'color': 'blue',
        }
    ]
    assert result['per_master'] == {'ps80': {'total': 0}, 'ps57': {'total': 1}}
    two_masters['ps80'].query_items.assert_called_once_with(fullname_pattern='ec2-instance', class_pattern=None)


@pytest.mark.asyncio
async def test_find_items_reports_a_failing_master_inline(two_masters):
    two_masters['ps80'].query_items.side_effect = RuntimeError('403 Forbidden')
    two_masters['ps57'].query_items.return_value = [_job('a')]

    result = await fleet.find_items(fullname_pattern='a')

    assert result['per_master']['ps80'] == {'error': 'RuntimeError: 403 Forbidden'}
    assert result['total'] == 1


@pytest.mark.asyncio
async def test_find_items_masters_subset_and_unknown_name(two_masters):
    two_masters['ps57'].query_items.return_value = []

    result = await fleet.find_items(fullname_pattern='a', masters=['ps57', 'ps3'])

    two_masters['ps80'].query_items.assert_not_called()
    assert result['per_master']['ps57'] == {'total': 0}
    assert 'unknown master' in result['per_master']['ps3']['error']


@pytest.mark.asyncio
async def test_find_items_honours_the_callers_master_scope(two_masters, mocker):
    mocker.patch('mcp_jenkins.server.fleet.allowed_masters', return_value=frozenset({'ps57'}))
    two_masters['ps57'].query_items.return_value = []

    result = await fleet.find_items(fullname_pattern='a')
    asked = await fleet.find_items(fullname_pattern='a', masters=['ps80'])

    two_masters['ps80'].query_items.assert_not_called()
    assert set(result['per_master']) == {'ps57'}
    assert asked['per_master'] == {'ps80': {'error': 'not reachable for this account'}}


@pytest.mark.asyncio
async def test_find_items_builds_clients_on_the_request_thread(two_masters, mocker):
    """Worker threads may not see the request's contextvars (the caller's token); they get a client
    already chosen on the request thread and never decide which masters are reachable."""
    import threading

    threads = []
    mocker.patch(
        'mcp_jenkins.server.fleet.client_for',
        side_effect=lambda name: threads.append(threading.current_thread()) or two_masters[name],
    )
    for client in two_masters.values():
        client.query_items.return_value = []

    await fleet.find_items(fullname_pattern='a')

    assert threads == [threading.main_thread()] * 2


@pytest.mark.asyncio
async def test_find_items_rejects_catch_all(two_masters):
    with pytest.raises(ValueError, match='narrowing fullname_pattern'):
        await fleet.find_items(fullname_pattern='.*')


@pytest.mark.asyncio
async def test_find_items_times_out_a_slow_master(two_masters, mocker):
    import time

    mocker.patch('mcp_jenkins.server.fleet._FIND_SECONDS', 0.2)
    two_masters['ps80'].query_items.side_effect = lambda **kw: time.sleep(1) or []
    two_masters['ps57'].query_items.return_value = [_job('a')]

    result = await fleet.find_items(fullname_pattern='a')

    assert result['per_master']['ps80'] == {'error': 'timed out after 0.2s'}
    assert result['per_master']['ps57'] == {'total': 1}


@pytest.mark.asyncio
async def test_find_items_truncates_at_limit(two_masters):
    two_masters['ps80'].query_items.return_value = [_job(f'j{i}') for i in range(3)]
    two_masters['ps57'].query_items.return_value = [_job('k')]

    result = await fleet.find_items(fullname_pattern='j|k', limit=2)

    assert (result['total'], result['returned'], result['truncated']) == (4, 2, True)
