import os
from typing import TYPE_CHECKING, Any, Literal

from fastmcp import FastMCP
from loguru import logger
from pydantic import AnyHttpUrl
from starlette.applications import Starlette
from starlette.middleware import Middleware as ASGIMiddleware
from starlette.requests import Request
from starlette.responses import PlainTextResponse, Response

from mcp_jenkins.core import AuthMiddleware, LifespanContext, lifespan

if TYPE_CHECKING:
    from fastmcp.server.auth import RemoteAuthProvider

__all__ = ['mcp']


def _build_auth() -> 'RemoteAuthProvider | None':
    """Build the OAuth2 resource-server auth provider from env, or None for local dev.

    When the MCP_OIDC_* vars are set, the server validates incoming bearer JWTs against the
    IdP (Authentik) JWKS and FastMCP auto-serves RFC 9728 protected-resource-metadata plus the
    401 WWW-Authenticate challenge. Unset means run open, for local development only.
    """
    issuer = os.getenv('MCP_OIDC_ISSUER')
    jwks_uri = os.getenv('MCP_OIDC_JWKS_URI')
    audience = os.getenv('MCP_OIDC_AUDIENCE')
    base_url = os.getenv('MCP_PUBLIC_BASE_URL')
    if not all((issuer, jwks_uri, audience, base_url)):
        logger.warning('MCP_OIDC_* not fully set; running WITHOUT auth (local dev only).')
        return None

    from fastmcp.server.auth import RemoteAuthProvider
    from fastmcp.server.auth.providers.jwt import JWTVerifier

    verifier = JWTVerifier(jwks_uri=jwks_uri, issuer=issuer, audience=audience)
    return RemoteAuthProvider(
        token_verifier=verifier,
        authorization_servers=[AnyHttpUrl(issuer)],
        base_url=base_url,
        scopes_supported=['openid', 'profile'],
        resource_name='Jenkins MCP',
    )


class JenkinsMCP(FastMCP[LifespanContext]):
    def http_app(
        self,
        path: str | None = None,
        middleware: list[ASGIMiddleware] | None = None,
        transport: Literal['http', 'streamable-http', 'sse'] = 'http',
        **kwargs: Any,  # noqa: ANN401
    ) -> 'Starlette':
        """Override to add JenkinsAuthMiddleware"""
        jenkins_auth_mw = ASGIMiddleware(AuthMiddleware)

        final_middleware_list = [jenkins_auth_mw]
        if middleware:
            final_middleware_list.extend(middleware)

        return super().http_app(path=path, middleware=final_middleware_list, transport=transport, **kwargs)


mcp = JenkinsMCP('mcp-jenkins', lifespan=lifespan, auth=_build_auth())


@mcp.custom_route('/healthz', methods=['GET'])
async def healthz(_request: Request) -> PlainTextResponse:
    """Liveness probe endpoint. Always returns 200 for kubernetes health checks."""
    return PlainTextResponse('OK', status_code=200)


@mcp.custom_route('/metrics', methods=['GET'])
async def metrics(_request: Request) -> Response:
    """Prometheus metrics endpoint: aggregate tool-call counters (no user labels)."""
    from prometheus_client import CONTENT_TYPE_LATEST, generate_latest

    return Response(generate_latest(), media_type=CONTENT_TYPE_LATEST)


# Register the per-request audit middleware (user attribution -> structured JSON -> Loki).
from mcp_jenkins.server.audit import AuditMiddleware  # noqa: E402

mcp.add_middleware(AuditMiddleware())


# Import tool modules to register them with the MCP server
# This must happen after mcp is created so the @mcp.tool() decorators can reference it
from mcp_jenkins.server import build, fleet, item, node, queue, readme, script, search, view  # noqa: F401, E402
