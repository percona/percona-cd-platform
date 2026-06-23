from starlette.types import ASGIApp, Receive, Scope, Send


class AuthMiddleware:
    """ASGI middleware that extracts the selected master NAME from the X-Jenkins-Master header.

    Only a master name is read, never credentials. The server holds the read-only token for each
    configured master and looks it up by name; the name is validated against the fleet allowlist
    in core.fleet.client_for. This is the token-free model: clients pick a master, the server owns
    the credentials.
    """

    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        # Pass through non-HTTP requests directly per ASGI spec
        if scope['type'] != 'http':
            await self.app(scope, receive, send)
            return

        # Bypass for the health probe so kubernetes can poll it without headers
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

        await self.app(scope_copy, receive, send)
