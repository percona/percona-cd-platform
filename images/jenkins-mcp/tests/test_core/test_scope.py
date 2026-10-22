import json

import pytest
from fastmcp import Client
from fastmcp.exceptions import ToolError
from loguru import logger

from mcp_jenkins.core import scope
from mcp_jenkins.core.fleet import Fleet, Master
from mcp_jenkins.core.lifespan import jenkins
from mcp_jenkins.server import fleet, mcp, readme

PMM_SCOPED = ['jenkins-mcp-service-accounts', 'jenkins-mcp-master-pmm']


def _token(mocker, groups):
    token = mocker.Mock(claims={'sub': 'u1', 'groups': groups}, client_id='jenkins-mcp')
    mocker.patch('mcp_jenkins.core.scope.get_access_token', return_value=token)
    return token


class TestAllowedMasters:
    def test_no_token_is_unscoped(self, mocker):
        mocker.patch('mcp_jenkins.core.scope.get_access_token', return_value=None)
        assert scope.allowed_masters() is None

    def test_human_without_scope_groups_is_unscoped(self, mocker):
        _token(mocker, ['percona', 'jenkins-mcp-writers'])
        assert scope.allowed_masters() is None

    def test_scope_groups_name_the_masters(self, mocker):
        _token(mocker, [*PMM_SCOPED, 'jenkins-mcp-master-ps80'])
        assert scope.allowed_masters() == frozenset({'pmm', 'ps80'})

    def test_service_account_without_scope_group_reaches_nothing(self, mocker):
        _token(mocker, ['jenkins-mcp-service-accounts'])
        assert scope.allowed_masters() == frozenset()

    def test_non_list_groups_claim_reaches_nothing(self, mocker):
        # A bare string would make `in` a substring test and could fail open.
        _token(mocker, 'jenkins-mcp-master-pmm')
        assert scope.allowed_masters() == frozenset()

    def test_check_master_names_the_reachable_masters(self, mocker):
        _token(mocker, PMM_SCOPED)
        scope.check_master('pmm')
        with pytest.raises(scope.MasterScopeError, match=r"master 'ps80'.*Reachable masters: pmm"):
            scope.check_master('ps80')

    def test_check_master_unscoped_service_account_says_none(self, mocker):
        _token(mocker, ['jenkins-mcp-service-accounts'])
        with pytest.raises(scope.MasterScopeError, match='Reachable masters: none'):
            scope.check_master('pmm')


class TestJenkinsScope:
    @pytest.fixture
    def mock_client_for(self, mocker):
        return mocker.patch('mcp_jenkins.core.lifespan.client_for')

    @pytest.fixture
    def mock_get_http_request(self, mocker):
        return mocker.patch('mcp_jenkins.core.lifespan.get_http_request')

    @pytest.fixture
    def mock_ctx(self, mocker):
        return mocker.Mock(
            request_context=mocker.Mock(
                lifespan_context=mocker.Mock(default_master='ps80', jenkins_session_singleton=True)
            ),
            session=mocker.Mock(jenkins_clients={}),
        )

    def test_argument_outside_scope_denied(self, mocker, mock_client_for, mock_get_http_request, mock_ctx):
        _token(mocker, PMM_SCOPED)
        with pytest.raises(scope.MasterScopeError):
            jenkins(mock_ctx, 'ps80')
        mock_client_for.assert_not_called()

    def test_header_outside_scope_denied(self, mocker, mock_client_for, mock_get_http_request, mock_ctx):
        _token(mocker, PMM_SCOPED)
        mock_get_http_request.return_value = mocker.Mock(state=mocker.Mock(jenkins_master='ps80'))
        with pytest.raises(scope.MasterScopeError):
            jenkins(mock_ctx)
        mock_client_for.assert_not_called()

    def test_cached_client_does_not_bypass_scope(self, mocker, mock_client_for, mock_get_http_request, mock_ctx):
        _token(mocker, PMM_SCOPED)
        mock_ctx.session.jenkins_clients = {'ps80': mocker.Mock()}
        with pytest.raises(scope.MasterScopeError):
            jenkins(mock_ctx, 'ps80')

    def test_single_scope_wins_over_server_default(self, mocker, mock_client_for, mock_get_http_request, mock_ctx):
        # No argument and no header: a pmm-only caller lands on pmm, not the ps80 default.
        _token(mocker, PMM_SCOPED)
        mock_get_http_request.side_effect = RuntimeError('no http request')

        jenkins(mock_ctx)

        mock_client_for.assert_called_once_with('pmm')

    def test_scoped_caller_reaches_its_master(self, mocker, mock_client_for, mock_get_http_request, mock_ctx):
        _token(mocker, PMM_SCOPED)
        assert jenkins(mock_ctx, 'pmm') == mock_client_for.return_value

    def test_unscoped_caller_keeps_default(self, mocker, mock_client_for, mock_get_http_request, mock_ctx):
        _token(mocker, ['percona'])
        mock_get_http_request.side_effect = RuntimeError('no http request')

        jenkins(mock_ctx)

        mock_client_for.assert_called_once_with('ps80')


@pytest.fixture
def two_masters(mocker):
    cfg = Fleet(
        masters=[
            Master(name='ps80', url='https://ps80.cd', username='svc', token='x'),  # noqa: S106
            Master(name='pmm', url='https://pmm.cd', username='svc', token='x'),  # noqa: S106
        ]
    )
    mocker.patch('mcp_jenkins.server.fleet.get_fleet', return_value=cfg)
    mocker.patch('mcp_jenkins.server.readme.get_fleet', return_value=cfg)
    mocker.patch('mcp_jenkins.server.fleet.client_for', return_value=mocker.Mock(get_version=lambda: '2.568.3'))
    return cfg


@pytest.mark.asyncio
async def test_list_masters_shows_only_reachable(mocker, two_masters):
    _token(mocker, PMM_SCOPED)
    assert [m['name'] for m in await fleet.list_masters()] == ['pmm']


@pytest.mark.asyncio
async def test_find_items_searches_only_reachable_masters(mocker, two_masters):
    _token(mocker, PMM_SCOPED)
    built = []
    mocker.patch(
        'mcp_jenkins.server.fleet.client_for',
        side_effect=lambda name: built.append(name) or mocker.Mock(query_items=lambda **kw: []),
    )

    result = await fleet.find_items(fullname_pattern='pmm3', masters=['ps80', 'pmm'])

    assert built == ['pmm']
    assert result['per_master'] == {'ps80': {'error': 'not reachable for this account'}, 'pmm': {'total': 0}}


@pytest.mark.asyncio
async def test_list_masters_unscoped_shows_fleet(mocker, two_masters):
    _token(mocker, ['percona'])
    assert [m['name'] for m in await fleet.list_masters()] == ['ps80', 'pmm']


@pytest.mark.asyncio
async def test_readme_lists_only_reachable(mocker, two_masters):
    _token(mocker, PMM_SCOPED)
    mocker.patch('mcp_jenkins.server.readme._served_tool_names', return_value=None)
    text = await readme.get_readme()
    assert 'pmm' in text
    assert 'ps80' not in text


@pytest.fixture
def capture_audit():
    captured: list[str] = []
    sink_id = logger.add(captured.append, filter=lambda r: r['extra'].get('audit', False), format='{message}')
    yield captured
    logger.remove(sink_id)


@pytest.mark.asyncio
async def test_denial_through_fastmcp_is_audited(mocker, capture_audit):
    """End to end through the real tool path: the denial reaches the client and the audit log."""
    _token(mocker, PMM_SCOPED)
    client = mocker.Mock()
    client.get_items.return_value = []
    client_for = mocker.patch('mcp_jenkins.core.lifespan.client_for', return_value=client)

    async with Client(mcp) as mcp_client:
        with pytest.raises(ToolError, match="cannot reach master 'ps80'"):
            await mcp_client.call_tool('get_all_items', {'master': 'ps80'})
        denied = json.loads(capture_audit[-1])
        await mcp_client.call_tool('get_all_items', {'master': 'pmm'})
        allowed = json.loads(capture_audit[-1])

    assert denied['status'] == 'denied'
    assert denied['master'] == 'ps80'
    assert allowed['status'] == 'ok'
    client_for.assert_called_once_with('pmm')
