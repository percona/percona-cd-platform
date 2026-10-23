"""Per-job build grants for service accounts, read from MCP_JENKINS_SERVICE_GRANTS (JSON).

A member of jenkins-mcp-service-accounts may call build_item without the writers group, but only on
a job listed for its username under the master the call resolves to. A listed job may require
parameters to be present and non-blank, and refuse named values, so a cleanup job can remove one
named resource but never run its delete-everything action. Nothing else in the operate tier is
covered. An unset variable grants nothing (fail closed), and a malformed one fails at load.

Shape: {"<username>": {"<master>": {"<job fullname>": {"require": ["P"], "deny": {"P": ["V"]}}}}}
"""

import json
import os
from functools import lru_cache

from fastmcp.server.dependencies import get_access_token
from pydantic import BaseModel, ConfigDict, TypeAdapter

from mcp_jenkins.core.scope import SERVICE_ACCOUNTS_GROUP


class JobRule(BaseModel):
    """Parameter rules for one granted job. An empty rule grants the job with any parameters."""

    model_config = ConfigDict(extra='forbid')

    require: list[str] = []
    deny: dict[str, list[str]] = {}


_Grants = dict[str, dict[str, dict[str, JobRule]]]


@lru_cache(maxsize=1)
def get_grants() -> _Grants:
    raw = os.getenv('MCP_JENKINS_SERVICE_GRANTS')
    if not raw:
        return {}
    try:
        return TypeAdapter(_Grants).validate_python(json.loads(raw))
    except Exception as e:
        msg = f'MCP_JENKINS_SERVICE_GRANTS is malformed: {e}'
        raise ValueError(msg) from e


def build_denial(master: str | None, arguments: dict) -> str | None:
    """Return why the caller may not start this build, or None when a grant covers it."""
    token = get_access_token()
    claims = (token.claims or {}) if token is not None else {}
    groups = claims.get('groups', [])
    if not isinstance(groups, list) or SERVICE_ACCOUNTS_GROUP not in groups:
        return 'the caller is not a service account'
    username = claims.get('preferred_username')
    fullname = arguments.get('fullname')
    rule = get_grants().get(username, {}).get(master or '', {}).get(fullname)
    if rule is None:
        return f'build of {fullname!r} on master {master!r} is not granted to {username!r}'
    data = arguments.get('data') if isinstance(arguments.get('data'), dict) else {}
    for name in rule.require:
        if not str(data.get(name, '')).strip():
            return f'{fullname!r} requires a non-blank {name} parameter'
    for name, values in rule.deny.items():
        if name in data and str(data[name]).strip() in values:
            return f'{fullname!r} refuses {name}={str(data[name]).strip()}'
    return None
