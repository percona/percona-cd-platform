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

from mcp_jenkins.core.fleet import get_fleet
from mcp_jenkins.core.grants import build_denial
from mcp_jenkins.core.scope import SERVICE_ACCOUNTS_GROUP, MasterScopeError, allowed_masters

# Raw config/script mutators (node config, script console): NEVER served. They stay tagged 'write'
# in the server modules, so read-only/operate/manage all exclude them. run_groovy_script is RCE.
_CONFIG_TOOLS = frozenset({'set_node_config', 'run_groovy_script'})
# Job-definition CRUD: served in operate mode under the 'manage' tag AND gated to the writers group.
# They create/update/delete a job's config.xml, distinct from build lifecycle.
_MANAGE_TOOLS = frozenset({'create_item', 'set_item_config', 'delete_item'})
# Build-lifecycle tools: served in operate mode AND gated to the writers group.
# They trigger/replay/stop/cancel builds; they never update or delete job config.
_OPERATE_TOOLS = frozenset({'build_item', 'replay_build', 'stop_build', 'stop_builds', 'cancel_queue_item'})
# Config-XML reads: get_item_config returns a job's raw config.xml, which routinely carries plaintext
# secrets (e.g. the <authToken> "trigger builds remotely" token), and search_item_configs greps many.
# Both are served as 'read' tools but gated to the writers group so they are NOT exposed to every
# authenticated SSO user. See PS-11341.
_CONFIG_READ_TOOLS = frozenset({'get_item_config', 'search_item_configs'})
# Every tool whose call requires the writers group: build lifecycle, job-config CRUD, config-write,
# config read. _CONFIG_TOOLS (run_groovy_script, set_node_config) are normally never registered (they
# are write-tagged and no mode serves them), but they are gated here too so a mode-filter regression
# that registered one keeps it writers-gated rather than open to any authenticated SSO user.
_GROUP_GATED = _OPERATE_TOOLS | _MANAGE_TOOLS | _CONFIG_TOOLS | _CONFIG_READ_TOOLS
# All MUTATING tools, for the is_write audit flag. get_item_config is gated but NOT a write, so it is
# deliberately excluded here and stays is_write=false in the audit record.
_WRITE_TOOLS = _OPERATE_TOOLS | _MANAGE_TOOLS | _CONFIG_TOOLS
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
        'include',
        'queue_id',
        'timeout_seconds',
        'masters',
        'archive_member',
        'context_lines',
        'glob',
        'include_error_details',
        'label_pattern',
        'lines',
        'literal',
        'max_cases',
        'max_entries',
        'max_error_chars',
        'max_inline_bytes',
        'max_matches',
        'member',
        'start_after_job',
        'status_filter',
        'time_budget_seconds',
        'context',
        'before',
        'after',
        'regex',
    }
)

# Bounds on what an audit line may carry about a failed or malformed call (names only, never values).
_MAX_KEY_LEN = 40
_MAX_DROPPED_KEYS = 10
_MAX_ERROR_DETAILS = 5

# Dedicated stdout sink: emit ONLY audit records, as one raw JSON line (parsed by Loki `| json`).
logger.add(sys.stdout, level='INFO', format='{message}', filter=lambda r: r['extra'].get('audit', False))

# Aggregate usage metrics (NO user label -> no cardinality blowup); exposed at /metrics. The
# `master` label is bounded by the fleet allowlist (~4-10 values) plus 'none', so it stays
# low-cardinality. Buckets extend past the prometheus default 10s ceiling because cross-region
# Jenkins REST calls and log/artifact exports routinely run longer.
_CALLS = Counter('mcp_tool_calls', 'Total MCP tool calls.', ['tool', 'status', 'is_write', 'master'])
_DURATION = Histogram(
    'mcp_tool_duration_seconds',
    'MCP tool call duration in seconds.',
    ['tool', 'is_write', 'master'],
    buckets=(0.1, 0.25, 0.5, 1.0, 2.5, 5.0, 10.0, 30.0, 60.0, 120.0),
)
_DENIED = Counter('mcp_authz_denied', 'Authorization denials on tool calls.', ['tool', 'reason'])


def _hash(value: str | None) -> str | None:
    return hashlib.sha256(value.encode()).hexdigest()[:16] if value else None


def _master_label(name: str | None) -> str:
    """Clamp the master to the fleet allowlist for the metric label (bounded cardinality).

    The raw master arg/header is caller-controlled, so an unclamped label would let a typo or
    abuse blow up Prometheus cardinality. Known fleet names pass through; unselected -> 'none';
    anything else -> 'invalid'. Only the metric label is clamped; the audit log keeps the raw value.
    """
    if not name:
        return 'none'
    return name if name in get_fleet().names() else 'invalid'


def _summarize_args(arguments: dict | None, tool: str | None = None) -> dict:
    """Allowlist summary: safe scalars verbatim, build-param KEYS only, everything else dropped.

    The NAMES of dropped keys are kept (never their values), so a call that failed on an argument the
    tool does not take (job_name instead of fullname) still shows what the client sent.
    """
    if not arguments:
        return {}
    summary: dict = {}
    dropped: list[str] = []
    for key, value in arguments.items():
        if key == 'pattern' and tool in _CONFIG_READ_TOOLS:
            # A config search can be for a secret value itself: log its fingerprint, never the text.
            summary['pattern_sha'] = _hash(str(value))
            summary['pattern_len'] = len(str(value))
        elif key in _SAFE_ARG_KEYS:
            summary[key] = value
        elif key == 'data' and isinstance(value, dict):
            summary['data_keys'] = sorted(value.keys())  # build-parameter NAMES, never values
        elif key == 'items' and isinstance(value, list):
            # stop_builds targets: job names and build numbers only, both already logged for stop_build.
            summary['items'] = [
                {'fullname': i.get('fullname'), 'number': i.get('number')} for i in value[:50] if isinstance(i, dict)
            ]
            summary['items_count'] = len(value)
        else:
            # Includes a data or items argument of the wrong type, which the tool will reject.
            dropped.append(str(key)[:_MAX_KEY_LEN])
    if dropped:
        summary['dropped_keys'] = sorted(dropped)[:_MAX_DROPPED_KEYS]
    return summary


def _error_detail(error: Exception) -> list[dict] | None:
    """Pydantic `loc` + `type` per failed argument (never the input value), or None.

    FastMCP raises its own ValidationError for bad arguments with the pydantic error as the cause; a
    pydantic error raised inside a tool body (an unexpected Jenkins payload) carries errors() itself.
    """
    for source in (error, error.__cause__):
        errors = getattr(source, 'errors', None)
        if not callable(errors):
            continue
        # Runs inside the middleware's except block: any surprise here must not mask the real error.
        try:
            return [
                {
                    'loc': '.'.join(str(part)[:_MAX_KEY_LEN] for part in item.get('loc', ())),
                    'type': str(item.get('type')),
                }
                for item in list(errors())[:_MAX_ERROR_DETAILS]
            ]
        except Exception:  # noqa: BLE001
            return None
    return None


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


def _grant_master() -> str | None:
    """The caller's only reachable master, used when the call selects none (same rule as core.lifespan)."""
    allowed = allowed_masters()
    return next(iter(allowed)) if allowed is not None and len(allowed) == 1 else None


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
            'args': _summarize_args(arguments, tool),
            **_identity(),
        }
        start = time.perf_counter()
        # A non-list `groups` claim would turn `not in` into a substring test and could fail open
        # (e.g. a bare 'x-jenkins-mcp-writers-y' string). Force list-membership semantics.
        claim_groups = record.get('groups')
        is_service = isinstance(claim_groups, list) and SERVICE_ACCOUNTS_GROUP in claim_groups
        # A service account never gets the writers tier, even when it is also put in that group.
        is_writer = isinstance(claim_groups, list) and _WRITERS_GROUP in claim_groups and not is_service
        denied = tool in _GROUP_GATED and not is_writer
        try:
            if denied and tool == 'build_item' and is_service:
                # A service account may start the jobs its grant lists, see core.grants.
                record['master'] = record['master'] or _grant_master()
                grant_denial = build_denial(record['master'], arguments or {})
                if grant_denial:
                    record['status'] = 'denied'
                    record['deny_reason'] = 'not_granted'
                    _DENIED.labels(tool=tool, reason='not_granted').inc()
                    msg = f"{grant_denial}. To change this account's build grants, ask in #opensource-jenkins."
                    raise ToolError(msg)
                denied = False
            if denied:
                record['status'] = 'denied'
                record['deny_reason'] = 'not_writer'
                _DENIED.labels(tool=tool, reason='not_writer').inc()
                msg = (
                    f'{tool} requires the {_WRITERS_GROUP} Authentik group. To get access, ask in '
                    f'#opensource-jenkins. Once added, disconnect and re-authenticate this MCP '
                    f'connection (reconnecting alone keeps the old group-less token).'
                )
                if tool in _MANAGE_TOOLS:
                    # Do not send manage callers through the onboarding loop for nothing: the
                    # backend grant is pending, so manage tools return 403 even for writers.
                    msg += (
                        ' Note: manage tools return 403 even for writers today (backend grant'
                        ' pending, PS-11341); manage job definitions via the Jenkins API/CLI'
                        ' instead.'
                    )
                raise ToolError(msg)
            result = await call_next(context)
            record['status'] = 'ok'
            # Dropped names only matter when the call failed (an argument the tool does not take).
            record['args'].pop('dropped_keys', None)
            return result
        except MasterScopeError:
            # The master scope is enforced where the tool resolves its master (core.lifespan),
            # which also covers the header and default paths. Recorded here as a denial.
            record['status'] = 'denied'
            record['deny_reason'] = 'master_scope'
            _DENIED.labels(tool=tool, reason='master_scope').inc()
            raise
        except Exception as e:  # noqa: BLE001
            if record.get('status') != 'denied':
                record['status'] = 'error'
                record['error'] = type(e).__name__
                record['error_module'] = type(e).__module__
                detail = _error_detail(e)
                if detail:
                    record['error_detail'] = detail
            raise
        finally:
            duration_s = time.perf_counter() - start
            record['duration_ms'] = round(duration_s * 1000, 1)
            is_write_label = 'true' if record['is_write'] else 'false'
            master_label = _master_label(record['master'])
            _CALLS.labels(tool=tool, status=record['status'], is_write=is_write_label, master=master_label).inc()
            _DURATION.labels(tool=tool, is_write=is_write_label, master=master_label).observe(duration_s)
            logger.bind(audit=True).info(json.dumps(record, default=str))
