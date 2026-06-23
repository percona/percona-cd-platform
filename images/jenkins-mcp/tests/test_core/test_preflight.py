from mcp_jenkins.core import fleet


def _one_master_fleet():
    return fleet.Fleet(masters=[fleet.Master(name='ps80', url='https://ps80.cd', username='svc', token='x')])  # noqa: S106


def test_write_preflight_passes_for_scoped_identity(mocker):
    mocker.patch('mcp_jenkins.core.fleet.get_fleet', return_value=_one_master_fleet())
    client = mocker.Mock()
    client._session.get.return_value = mocker.Mock(status_code=403)  # denied -> scoped
    mocker.patch('mcp_jenkins.core.fleet.client_for', return_value=client)

    assert fleet.write_preflight() == []


def test_write_preflight_refuses_over_privileged_identity(mocker):
    mocker.patch('mcp_jenkins.core.fleet.get_fleet', return_value=_one_master_fleet())
    client = mocker.Mock()
    client._session.get.return_value = mocker.Mock(status_code=200)  # has the perm
    mocker.patch('mcp_jenkins.core.fleet.client_for', return_value=client)

    violations = fleet.write_preflight()

    assert len(violations) == 2  # RunScripts + Create both flagged
    assert any('RunScripts' in v for v in violations)


def test_write_preflight_skips_unreachable_master(mocker):
    mocker.patch('mcp_jenkins.core.fleet.get_fleet', return_value=_one_master_fleet())
    client = mocker.Mock()
    client._session.get.side_effect = RuntimeError('connection refused')
    mocker.patch('mcp_jenkins.core.fleet.client_for', return_value=client)

    assert fleet.write_preflight() == []  # probe errors are logged + skipped, not violations
