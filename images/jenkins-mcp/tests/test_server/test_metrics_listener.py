import importlib
import socket
import time
import urllib.error
import urllib.request

import pytest
from click.testing import CliRunner
from prometheus_client import CONTENT_TYPE_LATEST
from starlette.testclient import TestClient

import mcp_jenkins.server as server_module
from mcp_jenkins import main
from mcp_jenkins.server import metrics_listener
from mcp_jenkins.server.metrics_listener import (
    METRICS_PORT_ENV,
    parse_metrics_port,
    start_metrics_listener,
    start_metrics_server,
)


def _get(url: str) -> tuple[int, str, bytes]:
    try:
        with urllib.request.urlopen(url, timeout=5) as response:  # noqa: S310
            return response.status, response.headers.get('Content-Type', ''), response.read()
    except urllib.error.HTTPError as error:
        return error.code, error.headers.get('Content-Type', ''), error.read()


def _free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
        probe.bind(('127.0.0.1', 0))
        return probe.getsockname()[1]


@pytest.fixture
def listener():
    server = start_metrics_server('127.0.0.1', 0)
    try:
        yield f'http://127.0.0.1:{server.server_address[1]}'
    finally:
        server.shutdown()
        server.server_close()


@pytest.fixture
def reload_server(monkeypatch):
    """Reload the server module under a given env, and restore the default module afterwards."""

    def _reload(value: str | None):  # noqa: ANN202
        if value is None:
            monkeypatch.delenv(METRICS_PORT_ENV, raising=False)
        else:
            monkeypatch.setenv(METRICS_PORT_ENV, value)
        return importlib.reload(server_module)

    yield _reload
    monkeypatch.delenv(METRICS_PORT_ENV, raising=False)
    importlib.reload(server_module)


# The main application port


@pytest.mark.parametrize('value', [None, '', '   '])
def test_main_app_serves_metrics_when_no_port_is_set(reload_server, value):
    module = reload_server(value)

    app = module.mcp.http_app(transport='http')
    response = TestClient(app).get('/metrics')

    assert '/metrics' in {route.path for route in app.routes}
    assert response.status_code == 200
    assert response.headers['content-type'] == CONTENT_TYPE_LATEST


def test_main_app_answers_404_for_metrics_when_a_port_is_set(reload_server):
    module = reload_server('9888')

    app = module.mcp.http_app(transport='http')
    client = TestClient(app)

    assert '/metrics' not in {route.path for route in app.routes}
    assert client.get('/metrics').status_code == 404
    assert client.get('/healthz').status_code == 200


# The separate listener


def test_listener_serves_prometheus_text_on_metrics(listener):
    status, content_type, body = _get(f'{listener}/metrics')

    assert status == 200
    assert content_type == CONTENT_TYPE_LATEST
    assert b'mcp_tool_calls_total' in body
    assert b'mcp_http_requests_total' in body


@pytest.mark.parametrize('path', ['/', '/mcp', '/mcp/', '/healthz', '/metrics/', '/metricsx', '/favicon.ico'])
def test_listener_answers_404_for_every_other_path(listener, path):
    status, _, body = _get(f'{listener}{path}')

    assert status == 404
    assert b'mcp_tool_calls' not in body


@pytest.mark.parametrize('sent', [b'', b'GET /metr'], ids=['silent', 'half-request-line'])
def test_listener_closes_a_stalled_connection(listener, monkeypatch, capfd, sent):
    monkeypatch.setattr(metrics_listener._MetricsHandler, 'timeout', 0.2)
    port = int(listener.rsplit(':', 1)[1])

    with socket.create_connection(('127.0.0.1', port), timeout=5) as client:
        client.sendall(sent)
        started = time.monotonic()
        assert client.recv(1024) == b''
        elapsed = time.monotonic() - started

    assert elapsed < 3
    assert 'Traceback' not in capfd.readouterr().err


def test_handler_timeout_is_ten_seconds():
    assert metrics_listener._MetricsHandler.timeout == 10


def test_listener_refuses_a_post_to_the_mcp_path(listener):
    request = urllib.request.Request(f'{listener}/mcp', data=b'{}', method='POST')  # noqa: S310
    try:
        urllib.request.urlopen(request, timeout=5)  # noqa: S310
    except urllib.error.HTTPError as error:
        assert error.code == 501
        assert b'mcp_tool_calls' not in error.read()
    else:
        pytest.fail('POST /mcp on the metrics listener must not succeed')


# Parsing of the configured value


@pytest.mark.parametrize(
    ('raw', 'expected'), [(None, None), ('', None), ('  ', None), ('9888', 9888), (' 9888 ', 9888)]
)
def test_parse_accepts_unset_and_valid_ports(raw, expected):
    assert parse_metrics_port(raw, main_port=9887) == expected


@pytest.mark.parametrize('raw', ['abc', '9888x', '98.8', '-1', '0', '65536', '+9888', '9887'])
def test_parse_rejects_bad_values(raw):
    with pytest.raises(ValueError, match=METRICS_PORT_ENV):
        parse_metrics_port(raw, main_port=9887)


def test_start_listener_returns_none_without_a_port(monkeypatch):
    monkeypatch.delenv(METRICS_PORT_ENV, raising=False)

    assert start_metrics_listener('127.0.0.1', 9887) is None


def test_start_listener_exits_on_a_bad_value(monkeypatch, mocker):
    monkeypatch.setenv(METRICS_PORT_ENV, '9887')
    mock_logger = mocker.patch('mcp_jenkins.server.metrics_listener.logger')

    with pytest.raises(SystemExit) as raised:
        start_metrics_listener('127.0.0.1', 9887)

    assert raised.value.code == 1
    assert METRICS_PORT_ENV in mock_logger.error.call_args.args[0]


# The command line start


def test_main_starts_the_listener_before_the_http_server(monkeypatch, mocker):
    port = _free_port()
    monkeypatch.setenv(METRICS_PORT_ENV, str(port))
    mock_asyncio = mocker.patch('mcp_jenkins.asyncio')
    mock_mcp = mocker.patch('mcp_jenkins.server.mcp')
    spy = mocker.spy(metrics_listener, 'start_metrics_server')

    def _run(_coroutine):  # noqa: ANN202
        # The listener must already answer when the main server starts.
        assert _get(f'http://127.0.0.1:{port}/metrics')[0] == 200

    mock_asyncio.run.side_effect = _run
    try:
        result = CliRunner().invoke(main, ['--transport', 'streamable-http', '--host', '127.0.0.1', '--port', '9887'])
        assert result.exit_code == 0, result.output
        spy.assert_called_once_with('127.0.0.1', port)
        mock_mcp.run_async.assert_called_once_with(transport='streamable-http', host='127.0.0.1', port=9887)
        mock_asyncio.run.assert_called_once()
    finally:
        if spy.spy_return is not None:
            spy.spy_return.shutdown()
            spy.spy_return.server_close()


@pytest.mark.parametrize('value', ['not-a-port', '9887', '70000'])
def test_main_refuses_to_start_with_a_bad_value(monkeypatch, mocker, value):
    monkeypatch.setenv(METRICS_PORT_ENV, value)
    mocker.patch('mcp_jenkins.asyncio')
    mock_mcp = mocker.patch('mcp_jenkins.server.mcp')

    result = CliRunner().invoke(main, ['--transport', 'streamable-http', '--host', '127.0.0.1', '--port', '9887'])

    assert result.exit_code == 1
    mock_mcp.run_async.assert_not_called()


def test_main_refuses_to_start_when_the_port_is_taken(monkeypatch, mocker):
    mocker.patch('mcp_jenkins.asyncio')
    mock_mcp = mocker.patch('mcp_jenkins.server.mcp')
    mock_logger = mocker.patch('mcp_jenkins.server.metrics_listener.logger')
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as holder:
        holder.bind(('127.0.0.1', 0))
        holder.listen(1)
        monkeypatch.setenv(METRICS_PORT_ENV, str(holder.getsockname()[1]))

        result = CliRunner().invoke(main, ['--transport', 'sse', '--host', '127.0.0.1', '--port', '9887'])

    assert result.exit_code == 1
    assert 'cannot bind' in mock_logger.error.call_args.args[0]
    mock_mcp.run_async.assert_not_called()


def test_main_stdio_starts_no_listener(monkeypatch, mocker):
    monkeypatch.setenv(METRICS_PORT_ENV, str(_free_port()))
    mocker.patch('mcp_jenkins.asyncio')
    mocker.patch('mcp_jenkins.server.mcp')
    spy = mocker.spy(metrics_listener, 'start_metrics_server')

    result = CliRunner().invoke(main, ['--transport', 'stdio'])

    assert result.exit_code == 0
    spy.assert_not_called()


def test_main_http_without_a_port_starts_no_listener(monkeypatch, mocker):
    monkeypatch.delenv(METRICS_PORT_ENV, raising=False)
    mocker.patch('mcp_jenkins.asyncio')
    mocker.patch('mcp_jenkins.server.mcp')
    spy = mocker.spy(metrics_listener, 'start_metrics_server')

    result = CliRunner().invoke(main, ['--transport', 'streamable-http', '--host', '127.0.0.1', '--port', '9887'])

    assert result.exit_code == 0
    spy.assert_not_called()
