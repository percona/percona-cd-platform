from prometheus_client import Counter
from starlette.types import ASGIApp, Message, Receive, Scope, Send

# HTTP-layer request counter. Defined here, not in server.audit, because this ASGI layer wraps the
# whole app and observes the 401/403 auth challenges FastMCP emits below the tool-call middleware
# (failed logins / bad tokens are invisible to the per-tool audit). `path` is collapsed to a fixed
# set so cardinality stays bounded.
_HTTP_REQUESTS = Counter('mcp_http_requests', 'Total HTTP requests to the MCP server.', ['path', 'status'])


def _bucket_path(path: str) -> str:
    """Collapse the request path to a fixed low-cardinality set for the metric label."""
    if path == '/metrics':
        return '/metrics'
    if path == '/mcp' or path.startswith('/mcp/'):
        return '/mcp'
    return 'other'


class AuthMiddleware:
    """ASGI middleware that extracts the selected master NAME from the X-Jenkins-Master header.

    Only a master name is read, never credentials. The server holds the read-only token for each
    configured master and looks it up by name; the name is validated against the fleet allowlist
    in core.fleet.client_for. This is the token-free model: clients pick a master, the server owns
    the credentials.

    It also counts every HTTP request by bucketed path and response status, so auth failures
    (401/403) that never reach the tool-call audit are still observable.
    """

    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        # Pass through non-HTTP requests directly per ASGI spec
        if scope['type'] != 'http':
            await self.app(scope, receive, send)
            return

        # Bypass for the health probe so kubernetes can poll it without headers (and without metrics noise)
        if scope.get('path') == '/healthz':
            await self.app(scope, receive, send)
            return

        # ASGI spec: copy scope when modifying it
        scope_copy: Scope = dict(scope)
        if 'state' not in scope_copy:
            scope_copy['state'] = {}

        headers = dict(scope_copy.get('headers', []))
        master_bytes = headers.get(b'x-jenkins-master')
        scope_copy['state']['jenkins_master'] = master_bytes.decode('latin-1') if master_bytes else None

        # Wrap send to capture the response status. This is the outermost middleware, so it sees the
        # final status including auth challenges (401/403) raised by the inner FastMCP auth layer.
        status_code = 0

        async def send_wrapper(message: Message) -> None:
            nonlocal status_code
            if message['type'] == 'http.response.start':
                status_code = message['status']
            await send(message)

        try:
            await self.app(scope_copy, receive, send_wrapper)
        finally:
            _HTTP_REQUESTS.labels(path=_bucket_path(scope.get('path', '')), status=str(status_code)).inc()
