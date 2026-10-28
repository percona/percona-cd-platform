"""Per-caller master scope, read from the groups claim of the validated token.

A caller in one or more `jenkins-mcp-master-<name>` groups reaches only those masters. A member of
jenkins-mcp-service-accounts must hold at least one such group, so an unscoped service account
reaches no master (fail closed). Any other caller keeps the whole fleet. No token (stdio transport,
local dev) means no scope, the HTTP transport rejects unauthenticated calls before any tool runs.
"""

from fastmcp.exceptions import ToolError
from fastmcp.server.dependencies import get_access_token

SCOPE_GROUP_PREFIX = 'jenkins-mcp-master-'
SERVICE_ACCOUNTS_GROUP = 'jenkins-mcp-service-accounts'


class MasterScopeError(ToolError):
    """The caller's token does not allow the selected master."""


def allowed_masters() -> frozenset[str] | None:
    """Return the masters the caller may reach, or None when the caller is not scoped."""
    token = get_access_token()
    if token is None:
        return None
    groups = (token.claims or {}).get('groups', [])
    # A non-list claim would turn membership into a substring test, so it reaches nothing.
    if not isinstance(groups, list):
        return frozenset()
    scoped = frozenset(
        g.removeprefix(SCOPE_GROUP_PREFIX) for g in groups if isinstance(g, str) and g.startswith(SCOPE_GROUP_PREFIX)
    )
    if scoped or SERVICE_ACCOUNTS_GROUP in groups:
        return scoped
    return None


def visible_masters(names: list[str]) -> list[str]:
    """Filter master names to the ones the caller may reach, keeping their order."""
    allowed = allowed_masters()
    return names if allowed is None else [n for n in names if n in allowed]


def check_master(name: str) -> None:
    """Raise MasterScopeError when the caller's scope excludes the named master."""
    allowed = allowed_masters()
    if allowed is not None and name not in allowed:
        reachable = ', '.join(sorted(allowed)) or 'none'
        msg = f'This account cannot reach master {name!r}. Reachable masters: {reachable}.'
        raise MasterScopeError(msg)
