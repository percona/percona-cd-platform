import pytest

from mcp_jenkins.core import AuthMiddleware


class TestAuthMiddleware:
    @pytest.mark.asyncio
    async def test_call(self, mocker):
        mock_app, mock_receive, mock_send = (mocker.AsyncMock(), mocker.AsyncMock(), mocker.AsyncMock())
        middleware = AuthMiddleware(mock_app)

        scope = {'type': 'http', 'path': '/mcp', 'headers': [(b'x-jenkins-master', b'ps80')]}

        await middleware(scope, mock_receive, mock_send)

        mock_app.assert_called_once()
        called_scope, called_receive, called_send = mock_app.call_args.args
        assert called_scope['state']['jenkins_master'] == 'ps80'
        assert called_receive is mock_receive
        # send is wrapped to capture the response status for the metric, so it is NOT the raw send
        assert called_send is not mock_send

    @pytest.mark.asyncio
    async def test_call_missing_header(self, mocker):
        mock_app, mock_receive, mock_send = (mocker.AsyncMock(), mocker.AsyncMock(), mocker.AsyncMock())
        middleware = AuthMiddleware(mock_app)

        scope = {'type': 'http', 'path': '/mcp'}

        await middleware(scope, mock_receive, mock_send)

        mock_app.assert_called_once()
        called_scope = mock_app.call_args.args[0]
        assert called_scope['state']['jenkins_master'] is None

    @pytest.mark.asyncio
    async def test_call_records_http_request_metric(self, mocker):
        from prometheus_client import REGISTRY

        async def app(scope, receive, send):
            await send({'type': 'http.response.start', 'status': 401, 'headers': []})
            await send({'type': 'http.response.body', 'body': b''})

        middleware = AuthMiddleware(app)
        sent = []

        async def send(message):
            sent.append(message)

        async def receive():
            return {'type': 'http.request'}

        scope = {'type': 'http', 'path': '/mcp', 'headers': []}
        labels = {'path': '/mcp', 'status': '401'}
        before = REGISTRY.get_sample_value('mcp_http_requests_total', labels) or 0.0
        await middleware(scope, receive, send)
        after = REGISTRY.get_sample_value('mcp_http_requests_total', labels) or 0.0

        assert after == before + 1
        assert sent[0]['status'] == 401  # response still flows through the wrapper

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
