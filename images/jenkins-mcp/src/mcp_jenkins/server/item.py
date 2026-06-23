import xml.etree.ElementTree as ET
from typing import Literal

from fastmcp import Context

from mcp_jenkins.core.lifespan import MasterArg, jenkins
from mcp_jenkins.server import mcp


@mcp.tool(tags=['read'])
async def get_all_items(ctx: Context, limit: int = 200, master: MasterArg = None) -> dict:
    """List jobs and folders on a master as a flat, compact list.

    Returns each item's fullname, class, and color only (NOT the nested folder tree or build
    detail), so the result stays small on large masters. To filter by name use query_items; for
    one item's full detail use get_item.

    Args:
        limit: Maximum items to return (capped at 2000). On a large master, narrow with
            query_items rather than raising this.

    Returns:
        A dict: items (the compact list), total (items found), returned, and truncated (bool).
    """
    limit = max(1, min(limit, 2000))
    items = [i.model_dump(exclude_none=True, exclude={'jobs', 'lastBuild'}) for i in jenkins(ctx, master).get_items()]
    return {
        'items': items[:limit],
        'total': len(items),
        'returned': min(len(items), limit),
        'truncated': len(items) > limit,
    }


@mcp.tool(tags=['read'])
async def get_item(ctx: Context, fullname: str, master: MasterArg = None) -> dict:
    """Get specific item from Jenkins

    Args:
        fullname: The fullname of the item

    Returns:
        The item
    """
    return jenkins(ctx, master).get_item(fullname=fullname).model_dump(exclude_none=True)


@mcp.tool(tags=['read'])
async def get_item_config(ctx: Context, fullname: str, master: MasterArg = None) -> str:
    """Get specific item config from Jenkins

    Args:
        fullname: The fullname of the item

    Returns:
        The config of the item
    """
    return jenkins(ctx, master).get_item_config(fullname=fullname)


@mcp.tool(tags=['write'])
async def set_item_config(ctx: Context, fullname: str, config_xml: str, master: MasterArg = None) -> None:
    """Set specific item config in Jenkins

    Args:
        fullname: The fullname of the item
        config_xml: The config XML of the item
    """
    jenkins(ctx, master).set_item_config(fullname=fullname, config_xml=config_xml)


@mcp.tool(tags=['read'])
async def query_items(
    ctx: Context,
    class_pattern: str = None,
    fullname_pattern: str = None,
    color_pattern: str = None,
    folder_depth: int | None = None,
    limit: int = 200,
    master: MasterArg = None,
) -> dict:
    """Query items by field patterns, returned as a flat, compact list.

    Args:
        class_pattern: The pattern of the _class
        fullname_pattern: The pattern of the fullname
        color_pattern: The pattern of the color
        folder_depth: The maximum depth of folders to traverse. If None, traverses all levels.
        limit: Maximum items to return (capped at 2000).

    Returns:
        A dict: items (compact: fullname, class, color), total (items found), returned, truncated.
    """
    limit = max(1, min(limit, 2000))
    items = [
        i.model_dump(exclude_none=True, exclude={'jobs', 'lastBuild'})
        for i in jenkins(ctx, master).query_items(
            class_pattern=class_pattern,
            fullname_pattern=fullname_pattern,
            color_pattern=color_pattern,
            folder_depth=folder_depth,
        )
    ]
    return {
        'items': items[:limit],
        'total': len(items),
        'returned': min(len(items), limit),
        'truncated': len(items) > limit,
    }


@mcp.tool(tags=['operate'])
async def build_item(
    ctx: Context,
    fullname: str,
    build_type: Literal['build', 'buildWithParameters'],
    data: dict | None = None,
    master: MasterArg = None,
) -> int:
    """Build an item in Jenkins

    Args:
        fullname: The fullname of the item
        data: The parameters to trigger the build with. Required if build_type is 'buildWithParameters'.
        build_type: If your item is configured with parameters, you must use 'buildWithParameters' as build_type.

    Returns:
        The queue item number of the item.
    """
    return jenkins(ctx, master).build_item(fullname=fullname, build_type=build_type, data=data)


@mcp.tool(tags=['read'])
async def get_item_parameters(ctx: Context, fullname: str, master: MasterArg = None) -> list[dict]:
    """Get the parameter definitions of a Jenkins job

    Args:
        fullname: The fullname of the item

    Returns:
        A list of parameter definitions, each containing name, type, defaultValue, and description
    """
    config_xml = jenkins(ctx, master).get_item_config(fullname=fullname)
    root = ET.fromstring(config_xml)

    params = []
    for param in root.iter('parameterDefinitions'):
        for definition in param:
            entry = {
                'name': definition.findtext('name', ''),
                'type': definition.tag,
                'defaultValue': definition.findtext('defaultValue', ''),
                'description': definition.findtext('description', ''),
            }
            params.append(entry)

    return params
