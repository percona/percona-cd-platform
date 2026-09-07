from starlette.testclient import TestClient

from mcp_jenkins.server import JenkinsMCP, mcp


def test_http_app(mocker):
    jm = JenkinsMCP('mcp-jenkins-test')

    mock_wm = mocker.Mock()
    mocker.patch('mcp_jenkins.server.ASGIMiddleware', return_value=mock_wm)

    assert jm.http_app(path='/mcp', middleware=[mock_wm], transport='http').user_middleware.count(mock_wm) == 2


def test_healthz_returns_200():
    client = TestClient(mcp.http_app(transport='http'))

    response = client.get('/healthz')

    assert response.status_code == 200
    assert response.text == 'OK'


def test_authed_http_surface_is_exactly_the_known_route_set(mocker, monkeypatch):
    # Gate on the unauthenticated HTTP surface: a dependency upgrade that auto-registers a new
    # route (or stops challenging /mcp) must fail here, not in production.
    monkeypatch.setenv('MCP_OIDC_ISSUER', 'https://idp.example/app/')
    monkeypatch.setenv('MCP_OIDC_JWKS_URI', 'https://idp.example/jwks/')
    monkeypatch.setenv('MCP_OIDC_AUDIENCE', 'jenkins-mcp')
    monkeypatch.setenv('MCP_PUBLIC_BASE_URL', 'https://jenkins-mcp.example')

    import importlib

    import mcp_jenkins.server as server_module

    authed_server = importlib.reload(server_module)
    try:
        app = authed_server.mcp.http_app(transport='streamable-http', stateless_http=True)
        paths = sorted({route.path for route in app.routes})
        assert paths == ['/.well-known/oauth-protected-resource/mcp', '/healthz', '/mcp', '/metrics']

        with TestClient(app, raise_server_exceptions=False) as client:
            response = client.post('/mcp', json={'jsonrpc': '2.0', 'method': 'initialize', 'id': 1})
            assert response.status_code == 401
            challenge = response.headers.get('www-authenticate', '')
            assert challenge.startswith('Bearer')
            assert 'resource_metadata=' in challenge
            assert client.get('/healthz').status_code == 200
            assert client.get('/metrics').status_code == 200
    finally:
        importlib.reload(server_module)
