from fastmcp import Context

from mcp_jenkins.core.lifespan import MasterArg, jenkins
from mcp_jenkins.server import mcp


@mcp.tool(tags=['read'])
async def get_all_queue_items(ctx: Context, master: MasterArg = None) -> list[dict]:
    """Get all items in Jenkins queue

    Returns:
        A list of all items in the Jenkins queue
    """
    return [item.model_dump(exclude_none=True, exclude={'task'}) for item in jenkins(ctx, master).get_queue().items]


@mcp.tool(tags=['read'])
async def get_queue_item(ctx: Context, id: int, master: MasterArg = None) -> dict:
    """Get a specific item in Jenkins queue by id

    Args:
        id: The id of the queue item

    Returns:
        The queue item
    """
    item = jenkins(ctx, master).get_queue_item(id=id, depth=1)
    return item.model_dump(exclude_none=True)


@mcp.tool(tags=['operate'])
async def cancel_queue_item(ctx: Context, id: int, master: MasterArg = None) -> None:
    """Cancel a specific item in Jenkins queue by id

    Args:
        id: The id of the queue item
    """
    jenkins(ctx, master).cancel_queue_item(id=id)
