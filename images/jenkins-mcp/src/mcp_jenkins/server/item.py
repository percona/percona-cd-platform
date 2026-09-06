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
    """Get a job/folder's raw config.xml (jenkins-mcp-writers only).

    Served as a read tool but gated per call to the jenkins-mcp-writers group (see audit.py),
    because config.xml can carry plaintext secrets such as the <authToken> "trigger builds remotely"
    token. Reads config.xml via the service identity's Job/ExtendedRead. get_readme covers how
    to join the group.

    Args:
        fullname: The fullname of the item

    Returns:
        The config XML of the item
    """
    return jenkins(ctx, master).get_item_config(fullname=fullname)


@mcp.tool(tags=['manage'])
async def set_item_config(ctx: Context, fullname: str, config_xml: str, master: MasterArg = None) -> None:
    """Update an existing item's config.xml in Jenkins (writers only).

    Gated to the jenkins-mcp-writers group (get_readme covers how to join) and served only in
    operate mode. Posts the full config.xml to an existing job; use create_item for an item that
    does not exist yet.

    Args:
        fullname: The fullname of the item
        config_xml: The full config XML to write
    """
    jenkins(ctx, master).set_item_config(fullname=fullname, config_xml=config_xml)


@mcp.tool(tags=['manage'])
async def create_item(ctx: Context, fullname: str, config_xml: str, master: MasterArg = None) -> None:
    """Create a new Jenkins item (job/folder) from a config.xml (writers only).

    Gated to the jenkins-mcp-writers group (get_readme covers how to join) and served only in
    operate mode. The parent folder
    must already exist; fails if an item with this fullname already exists (use set_item_config
    to update an existing one).

    Args:
        fullname: The fullname of the item to create
        config_xml: The full config XML for the new item
    """
    jenkins(ctx, master).create_item(fullname=fullname, config_xml=config_xml)


@mcp.tool(tags=['manage'])
async def delete_item(ctx: Context, fullname: str, master: MasterArg = None) -> None:
    """Delete a Jenkins item (job/folder) by fullname (writers only).

    Gated to the jenkins-mcp-writers group (get_readme covers how to join) and served only in
    operate mode. Irreversible: removes
    the item and its build history.

    Args:
        fullname: The fullname of the item to delete
    """
    jenkins(ctx, master).delete_item(fullname=fullname)


@mcp.tool(tags=['read'])
async def query_items(
    ctx: Context,
    class_pattern: str = None,
    fullname_pattern: str = None,
    color_pattern: str = None,
    label_pattern: str = None,
    folder_depth: int | None = None,
    limit: int = 200,
    master: MasterArg = None,
) -> dict:
    """Query items by field patterns, returned as a flat, compact list.

    Args:
        class_pattern: The pattern of the _class
        fullname_pattern: The pattern of the fullname
        color_pattern: The pattern of the color
        label_pattern: Regex on the job's assigned agent label (where arch lives, e.g.
            "docker-aarch64"). Matches FREESTYLE/MATRIX jobs that expose assignedLabel; PIPELINE
            (WorkflowJob) jobs keep their agent label inside the Jenkinsfile, so they never match,
            for those filter by fullname_pattern or read get_item_parameters.
        folder_depth: The maximum depth of folders to traverse. If None, traverses all levels.
        limit: Maximum items to return (capped at 2000).

    Returns:
        A dict: items (compact: fullname, class, color, and label when the job exposes one), total
        (items found), returned, truncated.
    """
    limit = max(1, min(limit, 2000))
    items = []
    for i in jenkins(ctx, master).query_items(
        class_pattern=class_pattern,
        fullname_pattern=fullname_pattern,
        color_pattern=color_pattern,
        label_pattern=label_pattern,
        folder_depth=folder_depth,
    ):
        entry = i.model_dump(exclude_none=True, exclude={'jobs', 'lastBuild', 'assignedLabel'})
        label = i.assigned_label_name()
        if label:
            entry['label'] = label
        items.append(entry)
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
    """Get the parameter definitions of a Jenkins job.

    Reads the job's parameterDefinitions via the standard job API (Job/Read), so it is open to all
    readers. For the full config.xml use get_item_config, which is gated to jenkins-mcp-writers.

    Args:
        fullname: The fullname of the item

    Returns:
        A list of parameter definitions, each with name, type, defaultValue, description, and
        choices (for choice parameters).
    """
    return jenkins(ctx, master).get_item_parameters(fullname=fullname)
