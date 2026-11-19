"""Prometheus metrics on a port of their own, apart from the MCP endpoint.

With MCP_METRICS_PORT set, a small stdlib HTTP server in a daemon thread answers GET /metrics on that
port and 404 on every other path, and the main application does not register /metrics. Unset or
empty, /metrics stays a route of the main application.
"""

import os
import socket
import sys
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from loguru import logger
from prometheus_client import CONTENT_TYPE_LATEST, REGISTRY, generate_latest

__all__ = [
    'METRICS_PORT_ENV',
    'metrics_port_configured',
    'parse_metrics_port',
    'start_metrics_listener',
    'start_metrics_server',
]

METRICS_PORT_ENV = 'MCP_METRICS_PORT'
METRICS_PATH = '/metrics'


def metrics_port_configured() -> bool:
    """True when MCP_METRICS_PORT holds a value. The value itself is checked at start."""
    return bool(os.getenv(METRICS_PORT_ENV, '').strip())


def parse_metrics_port(raw: str | None, main_port: int) -> int | None:
    """Return the metrics port, None when unset or empty. Raise ValueError for a bad value."""
    value = (raw or '').strip()
    if not value:
        return None
    if not (value.isascii() and value.isdigit()):
        raise ValueError(f'{METRICS_PORT_ENV}={value!r} is not a port number')
    port = int(value)
    if not 1 <= port <= 65535:
        raise ValueError(f'{METRICS_PORT_ENV}={port} is outside 1 to 65535')
    if port == main_port:
        raise ValueError(f'{METRICS_PORT_ENV}={port} equals the main port, set a different port')
    return port


class _MetricsHandler(BaseHTTPRequestHandler):
    """Answers GET /metrics with the default registry. Every other path is 404, other methods 501."""

    server_version = 'mcp-jenkins-metrics'
    sys_version = ''
    # Socket timeout in seconds. A client that stays silent is closed by handle_one_request, which
    # catches TimeoutError and logs it through log_message, silenced below.
    timeout = 10

    def do_GET(self) -> None:  # noqa: N802
        if self.path.split('?', 1)[0] != METRICS_PATH:
            self.send_error(404)
            return
        body = generate_latest(REGISTRY)
        self.send_response(200)
        self.send_header('Content-Type', CONTENT_TYPE_LATEST)
        self.send_header('Content-Length', str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, format: str, *args: object) -> None:
        """Silence the per-request stderr line. A scrape every 30 seconds is not worth a log line."""


class _MetricsServer(ThreadingHTTPServer):
    daemon_threads = True


class _MetricsServerV6(_MetricsServer):
    address_family = socket.AF_INET6


def start_metrics_server(host: str, port: int) -> ThreadingHTTPServer:
    """Bind host:port now and serve it from a daemon thread. Raise OSError when the bind fails."""
    server_class = _MetricsServerV6 if ':' in host else _MetricsServer
    server = server_class((host, port), _MetricsHandler)
    threading.Thread(target=server.serve_forever, name='metrics-listener', daemon=True).start()
    return server


def start_metrics_listener(host: str, main_port: int) -> ThreadingHTTPServer | None:
    """Start the listener from MCP_METRICS_PORT before the main server. Exit 1 on a bad value or bind."""
    try:
        port = parse_metrics_port(os.getenv(METRICS_PORT_ENV), main_port)
    except ValueError as error:
        logger.error(f'Refusing to start: {error}')
        sys.exit(1)
    if port is None:
        return None
    try:
        server = start_metrics_server(host, port)
    except OSError as error:
        logger.error(f'Refusing to start: {METRICS_PORT_ENV} cannot bind {host}:{port}: {error}')
        sys.exit(1)
    logger.info(f'Serving /metrics on {host}:{port}. The main port {main_port} does not serve it.')
    return server
