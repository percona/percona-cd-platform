"""Fleet-level tools: read across all configured masters at once.

These differ from the per-master tools: they iterate the server-held fleet config
(masters + read-only tokens) rather than a single connected master, so an agent can
discover the fleet and compare it without the user supplying any credentials.
"""

from loguru import logger

from mcp_jenkins.core.fleet import client_for, get_fleet
from mcp_jenkins.server import mcp


@mcp.tool(tags={'read'})
async def list_masters() -> list[dict]:
    """List the active Jenkins masters this server can reach, with a liveness check.

    Returns:
        One entry per configured master: name, url, reachable (bool), version (Jenkins core
        version when reachable), and error (a short message when the liveness check failed).
    """
    results = []
    for master in get_fleet().masters:
        entry: dict = {'name': master.name, 'url': master.url, 'reachable': False, 'version': None}
        try:
            entry['version'] = client_for(master.name).get_version()
            entry['reachable'] = True
        except Exception as e:  # noqa: BLE001
            entry['error'] = str(e)
            logger.warning(f'list_masters: {master.name} liveness failed: {e}')
        results.append(entry)
    return results
