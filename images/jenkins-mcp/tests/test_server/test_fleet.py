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
