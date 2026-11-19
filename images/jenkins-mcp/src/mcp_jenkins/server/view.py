from fastmcp import Context

from mcp_jenkins.core.lifespan import MasterArg, jenkins
from mcp_jenkins.server import mcp

# Each depth level makes Jenkins expand every job of the view one level further.
_MAX_VIEW_DEPTH = 2


@mcp.tool(tags=['read'])
def get_all_views(ctx: Context, master: MasterArg = None) -> list[dict]:
    """Get all top-level views from Jenkins.

    Returns:
        A list of views with their name and URL.
    """
    return jenkins(ctx, master).get_views()


@mcp.tool(tags=['read'])
def get_view(ctx: Context, view_path: str, depth: int = 0, master: MasterArg = None) -> dict:
    """Get a Jenkins view by path, returning its jobs and/or nested sub-views.

    Views can be nested up to multiple levels deep. Use "/" to separate levels
    in the path. If the view contains sub-views instead of jobs, the response
    will include their names so you can drill down further.

    Args:
        view_path: View path using "/" to separate levels.
                   Examples: "All", "frontend", "frontend/nightly".
                   Spaces and special characters in view names are handled automatically.
        depth: Depth of detail to retrieve for each job, 0 to 2. Default is 0. A value outside
               that range is refused.

    Returns:
        A dict with the view's name, jobs list, and/or nested views.
    """
    if not 0 <= depth <= _MAX_VIEW_DEPTH:
        msg = f'depth must be 0 to {_MAX_VIEW_DEPTH}, got {depth}'
        raise ValueError(msg)
    return jenkins(ctx, master).get_view(view_path=view_path, depth=depth)
