import threading

import pytest

from mcp_jenkins.core import fleet


@pytest.fixture(autouse=True)
def fresh_state(mocker):
    state = fleet._OperateState()
    mocker.patch.object(fleet, '_operate', state)
    return state


@pytest.fixture
def clock(mocker):
    """A settable monotonic clock for the re-probe interval."""
    now = {'t': 1000.0}
    mocker.patch.object(fleet, 'time', mocker.Mock(monotonic=lambda: now['t']))
    return now


def _one_master_fleet():
    return fleet.Fleet(masters=[fleet.Master(name='ps80', url='https://ps80.cd', username='svc', token='x')])  # noqa: S106


def _two_master_fleet():
    return fleet.Fleet(
        masters=[
            fleet.Master(name='ps80', url='https://ps80.cd', username='svc', token='x'),  # noqa: S106
            fleet.Master(name='pmm', url='https://pmm.cd', username='svc', token='x'),  # noqa: S106
        ]
    )


def _client(mocker, *, status=None, error=None):
    client = mocker.Mock()
    if error is not None:
        client._session.get.side_effect = error
    else:
        client._session.get.return_value = mocker.Mock(status_code=status)
    return client


def test_write_preflight_passes_for_scoped_identity(mocker):
    mocker.patch('mcp_jenkins.core.fleet.get_fleet', return_value=_one_master_fleet())
    mocker.patch('mcp_jenkins.core.fleet.client_for', return_value=_client(mocker, status=403))

    assert fleet.write_preflight() == []
    assert fleet.operate_status('ps80') == fleet.VERIFIED


def test_write_preflight_refuses_over_privileged_identity(mocker):
    mocker.patch('mcp_jenkins.core.fleet.get_fleet', return_value=_one_master_fleet())
    mocker.patch('mcp_jenkins.core.fleet.client_for', return_value=_client(mocker, status=200))

    violations = fleet.write_preflight()

    assert len(violations) == 2  # RunScripts + Create both flagged
    assert any('RunScripts' in v for v in violations)
    assert fleet.operate_status('ps80') == fleet.REFUSED


def test_probe_error_marks_the_master_not_verified(mocker):
    mocker.patch('mcp_jenkins.core.fleet.get_fleet', return_value=_one_master_fleet())
    mocker.patch('mcp_jenkins.core.fleet.client_for', return_value=_client(mocker, error=RuntimeError('refused')))

    assert fleet.write_preflight() == []  # an unreachable master never stops the gateway
    assert fleet.operate_status('ps80') == fleet.NOT_VERIFIED


@pytest.mark.parametrize('status', [404, 401, 302, 502, 503])
def test_answer_other_than_403_marks_the_master_not_verified(mocker, status):
    mocker.patch('mcp_jenkins.core.fleet.get_fleet', return_value=_one_master_fleet())
    mocker.patch('mcp_jenkins.core.fleet.client_for', return_value=_client(mocker, status=status))

    assert fleet.write_preflight() == []
    assert fleet.operate_status('ps80') == fleet.NOT_VERIFIED


def test_one_down_master_leaves_the_other_verified(mocker):
    mocker.patch('mcp_jenkins.core.fleet.get_fleet', return_value=_two_master_fleet())
    clients = {'ps80': _client(mocker, error=OSError('down')), 'pmm': _client(mocker, status=403)}
    mocker.patch('mcp_jenkins.core.fleet.client_for', side_effect=lambda name: clients[name])

    assert fleet.write_preflight() == []
    assert fleet.operate_status('ps80') == fleet.NOT_VERIFIED
    assert fleet.operate_status('pmm') == fleet.VERIFIED
    fleet.check_operate('pmm')


def test_status_is_not_enabled_before_any_preflight():
    assert fleet.operate_status('ps80') == fleet.OPERATE_OFF


class TestCheckOperate:
    @pytest.fixture
    def down_at_start(self, mocker, clock):
        """ps80 was unreachable at start. Set the 'client' key to change what later probes reach."""
        mocker.patch('mcp_jenkins.core.fleet.get_fleet', return_value=_one_master_fleet())
        current = {'client': _client(mocker, error=OSError('down'))}
        mocker.patch('mcp_jenkins.core.fleet.client_for', side_effect=lambda _name: current['client'])
        fleet.write_preflight()
        return current

    def test_verified_master_passes(self, mocker):
        mocker.patch('mcp_jenkins.core.fleet.get_fleet', return_value=_one_master_fleet())
        mocker.patch('mcp_jenkins.core.fleet.client_for', return_value=_client(mocker, status=403))
        fleet.write_preflight()

        fleet.check_operate('ps80')

    def test_not_verified_is_refused_inside_the_interval_without_a_probe(self, mocker, clock, down_at_start):
        down_at_start['client'] = good = _client(mocker, status=403)
        clock['t'] += fleet.REPROBE_SECONDS - 1

        with pytest.raises(fleet.OperateNotVerifiedError, match=r"master 'ps80'.*could not verify.*Read tools"):
            fleet.check_operate('ps80')
        good._session.get.assert_not_called()

    def test_recovers_after_a_later_good_probe(self, mocker, clock, down_at_start):
        down_at_start['client'] = good = _client(mocker, status=403)
        clock['t'] += fleet.REPROBE_SECONDS

        fleet.check_operate('ps80')

        assert fleet.operate_status('ps80') == fleet.VERIFIED
        assert good._session.get.call_count == 2
        fleet.check_operate('ps80')  # verified now, no further probe
        assert good._session.get.call_count == 2

    def test_failed_reprobe_waits_another_interval(self, clock, down_at_start):
        still_down = down_at_start['client']
        clock['t'] += fleet.REPROBE_SECONDS
        with pytest.raises(fleet.OperateNotVerifiedError):
            fleet.check_operate('ps80')
        probes = still_down._session.get.call_count

        clock['t'] += fleet.REPROBE_SECONDS - 1
        with pytest.raises(fleet.OperateNotVerifiedError):
            fleet.check_operate('ps80')
        assert still_down._session.get.call_count == probes

    def test_reprobe_that_finds_a_permission_refuses_the_master(self, mocker, clock, down_at_start):
        down_at_start['client'] = _client(mocker, status=200)
        clock['t'] += fleet.REPROBE_SECONDS

        with pytest.raises(fleet.OperateNotVerifiedError, match='holds a permission'):
            fleet.check_operate('ps80')
        assert fleet.operate_status('ps80') == fleet.REFUSED

        clock['t'] += 10 * fleet.REPROBE_SECONDS
        with pytest.raises(fleet.OperateNotVerifiedError, match='holds a permission'):
            fleet.check_operate('ps80')

    def test_concurrent_calls_send_one_probe(self, mocker, clock, down_at_start):
        gate = threading.Event()
        slow = mocker.Mock()

        def answer(*_args: object, **_kwargs: object) -> object:
            gate.wait(5)
            return mocker.Mock(status_code=403)

        slow._session.get.side_effect = answer
        down_at_start['client'] = slow
        clock['t'] += fleet.REPROBE_SECONDS
        outcomes: list[str] = []

        def call():
            try:
                fleet.check_operate('ps80')
                outcomes.append('ok')
            except fleet.OperateNotVerifiedError:
                outcomes.append('refused')

        threads = [threading.Thread(target=call) for _ in range(8)]
        for thread in threads:
            thread.start()
        gate.set()
        for thread in threads:
            thread.join(5)

        assert slow._session.get.call_count == 2  # one probe run, two endpoints
        assert 'ok' in outcomes
