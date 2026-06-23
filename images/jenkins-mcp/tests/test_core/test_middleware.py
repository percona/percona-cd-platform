import pytest

from mcp_jenkins.core import AuthMiddleware


class TestAuthMiddleware:
    @pytest.mark.asyncio
    async def test_call(self, mocker):
        mock_app, mock_receive, mock_send = (mocker.AsyncMock(), mocker.AsyncMock(), mocker.AsyncMock())
        middleware = AuthMiddleware(mock_app)

        scope = {'type': 'http', 'headers': [(b'x-jenkins-master', b'ps80')]}

        await middleware(scope, mock_receive, mock_send)

        mock_app.assert_called_once_with(
            {'type': 'http', 'headers': [(b'x-jenkins-master', b'ps80')], 'state': {'jenkins_master': 'ps80'}},
            mock_receive,
            mock_send,
        )

    @pytest.mark.asyncio
    async def test_call_missing_header(self, mocker):
        mock_app, mock_receive, mock_send = (mocker.AsyncMock(), mocker.AsyncMock(), mocker.AsyncMock())
        middleware = AuthMiddleware(mock_app)

        scope = {'type': 'http'}

        await middleware(scope, mock_receive, mock_send)

        mock_app.assert_called_once_with(
            {'type': 'http', 'state': {'jenkins_master': None}},
            mock_receive,
            mock_send,
        )

    @pytest.mark.asyncio
    async def test_call_non_http(self, mocker):
        mock_app, mock_receive, mock_send = (mocker.AsyncMock(), mocker.AsyncMock(), mocker.AsyncMock())
        middleware = AuthMiddleware(mock_app)

        scope = {'type': 'websocket'}

        await middleware(scope, mock_receive, mock_send)

        mock_app.assert_called_once_with(scope, mock_receive, mock_send)

    @pytest.mark.asyncio
    async def test_call_healthz_bypass(self, mocker):
        mock_app, mock_receive, mock_send = (mocker.AsyncMock(), mocker.AsyncMock(), mocker.AsyncMock())
        middleware = AuthMiddleware(mock_app)

        scope = {'type': 'http', 'path': '/healthz'}

        await middleware(scope, mock_receive, mock_send)

        mock_app.assert_called_once_with(scope, mock_receive, mock_send)
