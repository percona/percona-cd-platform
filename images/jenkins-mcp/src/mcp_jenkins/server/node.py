from fastmcp import Context

from mcp_jenkins.core.lifespan import MasterArg, jenkins
from mcp_jenkins.server import mcp


@mcp.tool(tags=['read'])
async def get_all_nodes(ctx: Context, master: MasterArg = None) -> list[dict]:
    """Get all nodes from Jenkins

    Returns:
        A list of all nodes
    """
    return [node.model_dump(exclude={'executors'}) for node in jenkins(ctx, master).get_nodes(depth=0)]


@mcp.tool(tags=['read'])
async def get_node(ctx: Context, name: str, master: MasterArg = None) -> dict:
    """Get a specific node from Jenkins

    Contains executor about the node.

    Args:
        name: The name of the node

    Returns:
        The node
    """
    return jenkins(ctx, master).get_node(name=name, depth=2).model_dump(exclude_none=True)


@mcp.tool(tags=['read'])
async def get_node_config(ctx: Context, name: str, master: MasterArg = None) -> str:
    """Get node config from Jenkins

    Args:
        name: The name of the node

    Returns:
        The config of the node
    """
    return jenkins(ctx, master).get_node_config(name=name)


@mcp.tool(tags=['write'])
async def set_node_config(ctx: Context, name: str, config_xml: str, master: MasterArg = None) -> None:
    """Set specific node config in Jenkins

    Args:
        name: The name of the node
        config_xml: The config XML of the node
    """
    jenkins(ctx, master).set_node_config(name=name, config_xml=config_xml)
