"""Per-request audit middleware: one structured JSON line per MCP tool call.

Records WHO (the authenticated user, from the validated JWT) called WHICH tool with WHAT
(an allowlisted argument summary), emitted to stdout where the cluster Alloy DaemonSet scrapes
it into Loki. This is the ONLY place per-tool-call attribution exists: the gateway holds a single
shared Jenkins credential, so Jenkins itself never sees the end user. Arguments pass through an
allowlist summariser, never logged raw, so build-parameter values, configs, and scripts cannot
leak. Query in Grafana: {namespace="jenkins-mcp"} | json | sub="<user>".
"""

import hashlib
import json
import sys
import time
import uuid
from typing import Any

from fastmcp.exceptions import ToolError
from fastmcp.server.dependencies import get_access_token, get_http_request
from fastmcp.server.middleware.middleware import Middleware, MiddlewareContext
from loguru import logger
from prometheus_client import Counter, Histogram

# Config/script-mutating tools (job UPDATE, node config, script console): NEVER served in operate
# mode; they stay tagged 'write' in the server modules so read-only/operate both exclude them.
_CONFIG_TOOLS = frozenset({'set_item_config', 'set_node_config', 'run_groovy_script'})
# Build-lifecycle tools: served only in write-enable (operate) mode AND only to the writers group.
# They trigger/replay/stop/cancel builds; they never update or delete job config.
_OPERATE_TOOLS = frozenset({'build_item', 'replay_build', 'stop_build', 'cancel_queue_item'})
_WRITE_TOOLS = _OPERATE_TOOLS | _CONFIG_TOOLS  # all mutating tools, for the is_write audit flag
_WRITERS_GROUP = 'jenkins-mcp-writers'

# Argument keys safe to log verbatim (job names, build numbers, search patterns, flags). Everything
# else is dropped; a build-parameter dict ('data') is reduced to its KEYS. Never log raw values.
_SAFE_ARG_KEYS = frozenset(
    {
        'fullname',
        'name',
        'number',
        'count',
        'view_path',
        'depth',
        'relative_path',
        'pattern',
        'offset',
        'limit',
        'id',
        'build_type',
        'class_pattern',
        'fullname_pattern',
        'color_pattern',
        'folder_depth',
        'master',
        'job_pattern',
        'ignore_case',
        'max_jobs',
        'builds_per_job',
        'matches_per_build',
        'max_total_matches',
    }
)

# Dedicated stdout sink: emit ONLY audit records, as one raw JSON line (parsed by Loki `| json`).
logger.add(sys.stdout, level='INFO', format='{message}', filter=lambda r: r['extra'].get('audit', False))

# Aggregate usage metrics (NO user label -> no cardinality blowup); exposed at /metrics.
_CALLS = Counter('mcp_tool_calls', 'Total MCP tool calls.', ['tool', 'status', 'is_write'])
_DURATION = Histogram('mcp_tool_duration_seconds', 'MCP tool call duration in seconds.', ['tool', 'is_write'])
_DENIED = Counter('mcp_authz_denied', 'Authorization denials on tool calls.', ['tool', 'reason'])


def _hash(value: str | None) -> str | None:
    return hashlib.sha256(value.encode()).hexdigest()[:16] if value else None


def _summarize_args(arguments: dict | None) -> dict:
    """Allowlist summary: safe scalars verbatim, build-param KEYS only, everything else dropped."""
    if not arguments:
        return {}
    summary: dict = {}
    for key, value in arguments.items():
        if key in _SAFE_ARG_KEYS:
            summary[key] = value
        elif key == 'data' and isinstance(value, dict):
            summary['data_keys'] = sorted(value.keys())  # build-parameter NAMES, never values
    return summary


def _identity() -> dict:
    """User identity from the validated JWT; graceful anonymous when unauthenticated (local dev)."""
    token = get_access_token()
    if token is None:
        return {'sub': 'anonymous'}
    claims = token.claims or {}
    return {
        'sub': claims.get('sub'),
        'preferred_username': claims.get('preferred_username'),
        'name': claims.get('name'),
        'groups': claims.get('groups', []),
        'email_hash': _hash(claims.get('email')),
        'client_id': token.client_id,
        'scopes': token.scopes,
        'aud': claims.get('aud'),
        'iss': claims.get('iss'),
        'iat': claims.get('iat'),
        'auth_time': claims.get('auth_time'),
        'jti_hash': _hash(claims.get('jti')),
        'token_exp': claims.get('exp'),
    }


def _selected_master(arguments: dict | None) -> str | None:
    if arguments and arguments.get('master'):
        return arguments['master']
    try:
        return getattr(get_http_request().state, 'jenkins_master', None)
    except RuntimeError:
        return None


class AuditMiddleware(Middleware):
    """Emit one structured JSON audit line per tool call (user + tool + summary + outcome)."""

    async def on_call_tool(self, context: MiddlewareContext, call_next: Any) -> Any:  # noqa: ANN401
        tool = context.message.name
        arguments = context.message.arguments
        record = {
            'event': 'mcp_tool_call',
            'request_id': uuid.uuid4().hex,
            'ts': context.timestamp.isoformat(),
            'tool': tool,
            'is_write': tool in _WRITE_TOOLS,
            'master': _selected_master(arguments),
            'args': _summarize_args(arguments),
            **_identity(),
        }
        start = time.perf_counter()
        denied = tool in _OPERATE_TOOLS and _WRITERS_GROUP not in (record.get('groups') or [])
        try:
            if denied:
                record['status'] = 'denied'
                _DENIED.labels(tool=tool, reason='not_writer').inc()
                msg = f'{tool} requires the {_WRITERS_GROUP} Authentik group.'
                raise ToolError(msg)
            result = await call_next(context)
            record['status'] = 'ok'
            return result
        except Exception as e:  # noqa: BLE001
            if record.get('status') != 'denied':
                record['status'] = 'error'
                record['error'] = type(e).__name__
            raise
        finally:
            duration_s = time.perf_counter() - start
            record['duration_ms'] = round(duration_s * 1000, 1)
            is_write_label = 'true' if record['is_write'] else 'false'
            _CALLS.labels(tool=tool, status=record['status'], is_write=is_write_label).inc()
            _DURATION.labels(tool=tool, is_write=is_write_label).observe(duration_s)
            logger.bind(audit=True).info(json.dumps(record, default=str))
