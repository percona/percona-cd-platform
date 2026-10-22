import re
import time
from typing import Literal

from fastmcp import Context
from requests.exceptions import RequestException

from mcp_jenkins.core.lifespan import MasterArg, jenkins
from mcp_jenkins.server import mcp
from mcp_jenkins.server.build_artifact_search import _build_matcher


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


# search_item_configs caps: jobs read per call, matches kept, and wall-clock time.
_MAX_CONFIG_JOBS = 100
_MAX_CONFIG_LINE = 500
_MAX_CONFIG_MATCHES_PER_JOB = 50
_MAX_CONFIG_MATCHES = 500
_CONFIG_SEARCH_SECONDS = 120
_CONFIG_CATCH_ALL = {'', '.*', '.+', '.*?', '*', '^.*$'}
# Matching lines are returned with secret-bearing element text and Jenkins-encrypted blobs blanked.
_SECRET_ELEMENT = re.compile(
    r'<([\w.-]*(?:authToken|password|passphrase|secret|token|apiKey)[\w.-]*)>[^<]*</\1>', re.IGNORECASE
)
_ENCRYPTED_BLOB = re.compile(r'\{AQAAAB[A-Za-z0-9+/=]+\}')


def _redact(line: str) -> str:
    line = _SECRET_ELEMENT.sub(r'<\1>[redacted]</\1>', line)
    return _ENCRYPTED_BLOB.sub('{[redacted]}', line)


@mcp.tool(tags=['read'])
def search_item_configs(
    ctx: Context,
    pattern: str,
    fullname_pattern: str,
    literal: bool = True,  # noqa: FBT001, FBT002
    ignore_case: bool = False,  # noqa: FBT001, FBT002
    limit: int = 100,
    master: MasterArg = None,
) -> dict:
    """Grep the config.xml of many jobs at once (jenkins-mcp-writers only), instead of get_item_config per job.

    Gated like get_item_config, because config.xml can carry secrets. Returns only the matching
    lines, never a whole config, and blanks secret-bearing elements (authToken, password, secret,
    token, apiKey) and Jenkins-encrypted values in them.

    Args:
        pattern: Substring (default) or regex (set literal=False) matched per config.xml line
        fullname_pattern: Regex selecting the jobs to read (required; a catch-all like ".*" is rejected)
        literal: Treat pattern as a literal substring (default)
        ignore_case: Case-insensitive match
        limit: Maximum jobs to read (capped at 100)

    Returns:
        A dict: jobs [{fullname, matches [{line_no, line}], truncated?}] for jobs with a match,
        errors [{fullname, error}], jobs_selected, jobs_read, match_count, notes.
    """
    if fullname_pattern.strip() in _CONFIG_CATCH_ALL:
        msg = 'search_item_configs needs a narrowing fullname_pattern (a catch-all would read every config)'
        raise ValueError(msg)
    matcher = _build_matcher(pattern, literal, ignore_case)
    limit = max(1, min(limit, _MAX_CONFIG_JOBS))
    client = jenkins(ctx, master)
    selected = [i.fullname for i in client.query_items(fullname_pattern=fullname_pattern) if i.fullname]
    notes: list[str] = []
    if len(selected) > limit:
        notes.append(f'{len(selected)} jobs matched fullname_pattern, only the first {limit} were read')
    jobs: list[dict] = []
    errors: list[dict] = []
    read = total = 0
    deadline = time.monotonic() + _CONFIG_SEARCH_SECONDS
    for fullname in selected[:limit]:
        if total >= _MAX_CONFIG_MATCHES or time.monotonic() > deadline:
            notes.append(f'stopped before {fullname}: match or time budget spent')
            break
        try:
            config = client.get_item_config(fullname=fullname)
        except RequestException as e:
            errors.append({'fullname': fullname, 'error': f'{type(e).__name__}: {e}'[:300]})
            continue
        read += 1
        hits = [
            {'line_no': n, 'line': _redact(line.strip())[:_MAX_CONFIG_LINE]}
            for n, line in enumerate(config.splitlines(), 1)
            if matcher(line)
        ]
        if not hits:
            continue
        keep = min(len(hits), _MAX_CONFIG_MATCHES_PER_JOB, _MAX_CONFIG_MATCHES - total)
        entry = {'fullname': fullname, 'matches': hits[:keep]}
        if keep < len(hits):
            entry['truncated'] = True
        jobs.append(entry)
        total += keep
    return {
        'jobs': jobs,
        'errors': errors,
        'jobs_selected': len(selected),
        'jobs_read': read,
        'match_count': total,
        'notes': notes,
    }


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
        The queue item number of the item. Pass it to wait_for_build(fullname, queue_id=...) to get
        the build number once the build starts.
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
