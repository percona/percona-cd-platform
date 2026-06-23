import pytest

from mcp_jenkins.core.fleet import Fleet, Master
from mcp_jenkins.core.lifespan import jenkins, lifespan


class TestLifespan:
    @pytest.mark.asyncio
    async def test_lifespan_context(self, mocker):
        def getenv(key, default=None):
            return {'jenkins_master': 'ps80', 'jenkins_session_singleton': 'true'}.get(key, default)

        mocker.patch('mcp_jenkins.core.lifespan.os', mocker.Mock(getenv=getenv))
        async with lifespan(mocker.Mock()) as context:
            assert context.default_master == 'ps80'
            assert context.jenkins_session_singleton is True


class TestJenkins:
    @pytest.fixture
    def mock_client_for(self, mocker):
        return mocker.patch('mcp_jenkins.core.lifespan.client_for')

    @pytest.fixture
    def mock_get_fleet(self, mocker):
        return mocker.patch('mcp_jenkins.core.lifespan.get_fleet')

    @pytest.fixture
    def mock_get_http_request(self, mocker):
        return mocker.patch('mcp_jenkins.core.lifespan.get_http_request')

    @pytest.fixture
    def mock_ctx(self, mocker):
        return mocker.Mock(
            request_context=mocker.Mock(
                lifespan_context=mocker.Mock(default_master=None, jenkins_session_singleton=False)
            )
        )

    def test_master_from_header(self, mock_client_for, mock_get_fleet, mock_get_http_request, mock_ctx, mocker):
        mock_get_http_request.return_value = mocker.Mock(state=mocker.Mock(jenkins_master='ps80'))

        result = jenkins(mock_ctx)

        mock_client_for.assert_called_once_with('ps80')
        assert result == mock_client_for.return_value

    def test_master_arg_overrides_header(
        self, mock_client_for, mock_get_fleet, mock_get_http_request, mock_ctx, mocker
    ):
        # An explicit per-call master argument wins over the header pin and the default.
        mock_get_http_request.return_value = mocker.Mock(state=mocker.Mock(jenkins_master='ps80'))
        mock_ctx.request_context.lifespan_context.default_master = 'ps57'

        jenkins(mock_ctx, 'pxc')

        mock_client_for.assert_called_once_with('pxc')

    def test_master_from_default(self, mock_client_for, mock_get_fleet, mock_get_http_request, mock_ctx):
        mock_get_http_request.side_effect = RuntimeError('no http request')
        mock_ctx.request_context.lifespan_context.default_master = 'ps57'

        jenkins(mock_ctx)

        mock_client_for.assert_called_once_with('ps57')

    def test_single_master_fallback(self, mock_client_for, mock_get_fleet, mock_get_http_request, mock_ctx):
        mock_get_http_request.side_effect = RuntimeError('no http request')
        mock_get_fleet.return_value = Fleet(masters=[Master(name='solo', url='u', username='u', token='x')])  # noqa: S106

        jenkins(mock_ctx)

        mock_client_for.assert_called_once_with('solo')

    def test_no_master_multi_fleet_raises(self, mock_client_for, mock_get_fleet, mock_get_http_request, mock_ctx):
        mock_get_http_request.side_effect = RuntimeError('no http request')
        mock_get_fleet.return_value = Fleet(
            masters=[
                Master(name='a', url='u', username='u', token='x'),  # noqa: S106
                Master(name='b', url='u', username='u', token='x'),  # noqa: S106
            ]
        )

        with pytest.raises(ValueError):
            jenkins(mock_ctx)
        mock_client_for.assert_not_called()

    def test_session_singleton_cache_hit(self, mock_client_for, mock_get_http_request, mock_ctx, mocker):
        mock_get_http_request.return_value = mocker.Mock(state=mocker.Mock(jenkins_master='ps80'))
        mock_ctx.request_context.lifespan_context.jenkins_session_singleton = True
        existing = mocker.Mock()
        mock_ctx.session.jenkins_clients = {'ps80': existing}

        assert jenkins(mock_ctx) == existing
        mock_client_for.assert_not_called()
