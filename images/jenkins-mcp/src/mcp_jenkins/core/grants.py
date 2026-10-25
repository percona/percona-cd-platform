"""Per-job build grants for service accounts, read from MCP_JENKINS_SERVICE_GRANTS (JSON).

A member of jenkins-mcp-service-accounts may call build_item without the writers group, but only on
a job listed for its username under the master the call resolves to. A listed job may carry
parameter rules, so a cleanup job can remove one named resource but never run its delete-everything
action. Nothing else in the operate tier is covered. An unset variable grants nothing (fail closed),
and a malformed one fails at load.

Rules, checked against the parameters Jenkins will receive:
- require: [P]       P must be present and non-blank.
- allow: {P: [V]}    when P is present its value must be one of V (pair with require to force it).
- pattern: {P: RE}   when P is present its value must fully match RE.

Every parameter value must be a string, since requests form-encodes a list as repeated fields that
Jenkins reads as a plain value. A job with rules must use buildWithParameters: the /build endpoint
reads parameters from a submitted `json` form field that the rules would never see.

Format: {"<username>": {"<master>": {"<job fullname>": {"require": [...], "allow": {...}, "pattern": {...}}}}}
"""

import json
import os
import re
from functools import lru_cache

from fastmcp.server.dependencies import get_access_token
from pydantic import BaseModel, ConfigDict, TypeAdapter, field_validator

from mcp_jenkins.core.scope import SERVICE_ACCOUNTS_GROUP


class JobRule(BaseModel):
    """Parameter rules for one granted job. An empty rule grants the job with any string parameters."""

    model_config = ConfigDict(extra='forbid')

    require: list[str] = []
    allow: dict[str, list[str]] = {}
    pattern: dict[str, str] = {}

    @field_validator('pattern')
    @classmethod
    def _compiles(cls, value: dict[str, str]) -> dict[str, str]:
        for regex in value.values():
            re.compile(regex)
        return value

    def has_rules(self) -> bool:
        return bool(self.require or self.allow or self.pattern)


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


def build_denial(master: object, arguments: dict) -> str | None:
    """Return why the caller may not start this build, or None when a grant covers it."""
    token = get_access_token()
    claims = (token.claims or {}) if token is not None else {}
    groups = claims.get('groups', [])
    if not isinstance(groups, list) or SERVICE_ACCOUNTS_GROUP not in groups:
        return 'the caller is not a service account'
    username = claims.get('preferred_username')
    fullname = arguments.get('fullname')
    if not isinstance(username, str) or not isinstance(fullname, str):
        return 'build is not granted: the account or job is missing'
    if not isinstance(master, str):
        return 'build is not granted: pass master=<name>, this account can reach more than one master'
    rule = get_grants().get(username, {}).get(master, {}).get(fullname)
    if rule is None:
        return f'build of {fullname!r} on master {master!r} is not granted to {username!r}'
    data = arguments.get('data')
    if data is None:
        data = {}
    if not isinstance(data, dict) or not all(isinstance(k, str) and isinstance(v, str) for k, v in data.items()):
        return f'{fullname!r} takes string parameter values only'
    if rule.has_rules() and arguments.get('build_type') != 'buildWithParameters':
        return f'{fullname!r} has parameter rules and must be started with buildWithParameters'
    for name in rule.require:
        if not data.get(name, '').strip():
            return f'{fullname!r} requires a non-blank {name} parameter'
    for name, values in rule.allow.items():
        if name in data and data[name] not in values:
            return f'{fullname!r} allows {name} only as one of {values}'
    for name, regex in rule.pattern.items():
        if name in data and not re.fullmatch(regex, data[name]):
            return f'{fullname!r} refuses this {name} value'
    return None
