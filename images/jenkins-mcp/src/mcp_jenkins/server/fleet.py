"""Fleet-level tools: read across all configured masters at once.

These differ from the per-master tools: they iterate the server-held fleet config
(masters + read-only tokens) rather than a single connected master, so an agent can
discover the fleet and compare it without the user supplying any credentials.
"""

import functools

import anyio
from loguru import logger

from mcp_jenkins.core.fleet import client_for, get_fleet
from mcp_jenkins.core.scope import allowed_masters
from mcp_jenkins.server import mcp

_MAX_FIND_ITEMS = 2000
# Whole-call budget: a master still crawling its job tree at this point is reported as timed out.
_FIND_SECONDS = 30
# Patterns that would list every job on every master; find_items is for locating jobs.
_FIND_CATCH_ALL = {'', '.*', '.+', '.*?', '*', '^.*$'}


@mcp.tool(tags={'read'})
async def list_masters() -> list[dict]:
    """List the active Jenkins masters this server can reach, with a liveness check.

    Returns:
        One entry per configured master the caller may reach: name, url, reachable (bool), version (Jenkins core
        version when reachable), and error (a short message when the liveness check failed).
    """
    results = []
    allowed = allowed_masters()
    for master in get_fleet().masters:
        if allowed is not None and master.name not in allowed:
            continue
        entry: dict = {'name': master.name, 'url': master.url, 'reachable': False, 'version': None}
        try:
            entry['version'] = client_for(master.name).get_version()
            entry['reachable'] = True
        except Exception as e:  # noqa: BLE001
            entry['error'] = str(e)
            logger.warning(f'list_masters: {master.name} liveness failed: {e}')
        results.append(entry)
    return results


@mcp.tool(tags={'read'})
async def find_items(
    fullname_pattern: str,
    class_pattern: str | None = None,
    masters: list[str] | None = None,
    limit: int = 200,
) -> dict:
    """Find which masters host jobs matching a pattern, searching every reachable master at once.

    Use it when you do not know the master: one call instead of query_items once per master.
    Masters are searched concurrently; a master that fails or runs past the 30 s budget is reported
    in per_master and never hides the others' results.

    Args:
        fullname_pattern: Regex on the job fullname (required; a catch-all like ".*" is rejected)
        class_pattern: Optional regex on the job class (e.g. "WorkflowJob", "Matrix")
        masters: Optional subset of master names to search (default: every master you can reach)
        limit: Maximum items returned across all masters (capped at 2000)

    Returns:
        A dict: items [{master, fullname, class_, color, url}], per_master {name: {total} or {error}},
        total (matches across masters), returned, truncated.
    """
    if fullname_pattern.strip() in _FIND_CATCH_ALL:
        msg = 'find_items needs a narrowing fullname_pattern; to list one master use query_items(master=...)'
        raise ValueError(msg)
    limit = max(1, min(limit, _MAX_FIND_ITEMS))
    fleet_names = get_fleet().names()
    # Scope is read here, on the request's own context: worker threads below never decide reachability.
    allowed = allowed_masters()
    reachable = [n for n in fleet_names if allowed is None or n in allowed]
    per_master: dict[str, dict] = {}
    targets = reachable
    if masters is not None:
        targets = [n for n in masters if n in reachable]
        for name in masters:
            if name not in fleet_names:
                per_master[name] = {'error': f'unknown master; configured: {fleet_names}'}
            elif name not in reachable:
                per_master[name] = {'error': 'not reachable for this account'}

    found: dict[str, list] = {}

    async def search(name: str) -> None:
        try:
            client = client_for(name)
            call = functools.partial(client.query_items, fullname_pattern=fullname_pattern, class_pattern=class_pattern)
            found[name] = await anyio.to_thread.run_sync(call, abandon_on_cancel=True)
            per_master[name] = {'total': len(found[name])}
        except Exception as e:  # noqa: BLE001
            per_master[name] = {'error': f'{type(e).__name__}: {e}'[:300]}
            logger.warning(f'find_items: {name} failed: {e}')

    with anyio.move_on_after(_FIND_SECONDS):
        async with anyio.create_task_group() as tg:
            for name in targets:
                tg.start_soon(search, name)
    for name in targets:
        per_master.setdefault(name, {'error': f'timed out after {_FIND_SECONDS}s'})

    items = [
        {'master': name, **i.model_dump(exclude_none=True, exclude={'jobs', 'lastBuild', 'assignedLabel', 'name'})}
        for name in targets
        for i in found.get(name, [])
    ]
    return {
        'items': items[:limit],
        'per_master': per_master,
        'total': len(items),
        'returned': min(len(items), limit),
        'truncated': len(items) > limit,
    }
