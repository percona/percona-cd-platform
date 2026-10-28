import json
import re
import time
from collections import deque
from concurrent.futures import ThreadPoolExecutor
from concurrent.futures import TimeoutError as FutureTimeout
from datetime import UTC, datetime
from typing import Literal

from fastmcp import Context
from requests.exceptions import RequestException

from mcp_jenkins.core.lifespan import MasterArg, jenkins
from mcp_jenkins.jenkins.rest_client import require_narrow_pattern
from mcp_jenkins.server import mcp
from mcp_jenkins.server.build_artifact_search import _build_matcher


@mcp.tool(tags=['read'])
def get_all_items(ctx: Context, limit: int = 200, master: MasterArg = None) -> dict:
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


# search_item_configs caps: jobs read per call, matches kept, and wall-clock time. Configs are
# read concurrently, one request each, so a whole master fits the time budget.
_MAX_CONFIG_JOBS = 500
_CONFIG_WORKERS = 8
_config_pool = ThreadPoolExecutor(max_workers=_CONFIG_WORKERS, thread_name_prefix='config-scan')
_MAX_CONFIG_LINE = 500
_MAX_CONFIG_MATCHES_PER_JOB = 50
_MAX_CONFIG_MATCHES = 500
_CONFIG_SEARCH_SECONDS = 120
# Matching lines are returned with secret-bearing element text and Jenkins-encrypted blobs blanked.
_SECRET_ELEMENT = re.compile(
    r'<([\w.-]*(?:authToken|password|passwd|passphrase|secret|token|apiKey|privateKey|accessKey)[\w.-]*)(\s[^>]*)?>'
    r'[^<]*</\1>',
    re.IGNORECASE,
)
_ENCRYPTED_BLOB = re.compile(r'\{AQAAAB[A-Za-z0-9+/=]+\}')


def _redact(line: str) -> str:
    line = _SECRET_ELEMENT.sub(r'<\1\2>[redacted]</\1>', line)
    return _ENCRYPTED_BLOB.sub('{[redacted]}', line)


@mcp.tool(tags=['read'])
def search_item_configs(
    ctx: Context,
    pattern: str,
    fullname_pattern: str,
    literal: bool = True,  # noqa: FBT001, FBT002
    ignore_case: bool = False,  # noqa: FBT001, FBT002
    limit: int = _MAX_CONFIG_JOBS,
    offset: int = 0,
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
        limit: Maximum jobs to read (capped at 500, read 8 at a time)
        offset: Skip this many selected jobs, to resume where a note says the last call stopped

    Returns:
        A dict: jobs [{fullname, matches [{line_no, line}], truncated?}] for jobs with a match,
        errors [{fullname, error}], jobs_selected, jobs_read, match_count, notes.
    """
    require_narrow_pattern(fullname_pattern, 'search_item_configs')
    matcher = _build_matcher(pattern, literal, ignore_case)
    limit = max(1, min(limit, _MAX_CONFIG_JOBS))
    offset = max(0, offset)
    client = jenkins(ctx, master)
    selected = [i.fullname for i in client.query_items(fullname_pattern=fullname_pattern) if i.fullname]
    batch = selected[offset : offset + limit]
    notes: list[str] = []
    if offset + limit < len(selected):
        notes.append(
            f'{len(selected)} jobs matched fullname_pattern, read {offset} to {offset + len(batch) - 1}, '
            f'call again with offset={offset + len(batch)} for the rest'
        )
    jobs: list[dict] = []
    errors: list[dict] = []
    read = total = 0
    deadline = time.monotonic() + _CONFIG_SEARCH_SECONDS
    # At most _CONFIG_WORKERS reads in flight per call, so one slow scan cannot queue hundreds of
    # reads ahead of another caller in the shared pool.
    pending: deque = deque()
    queued = iter(batch)

    def refill() -> None:
        while len(pending) < _CONFIG_WORKERS:
            fullname = next(queued, None)
            if fullname is None:
                return
            pending.append((fullname, _config_pool.submit(client.get_item_config, fullname=fullname)))

    try:
        refill()
        index = 0
        while pending:
            fullname, future = pending[0]  # stays pending until read, so a break still cancels it
            if total >= _MAX_CONFIG_MATCHES:
                notes.append(f'stopped before {fullname}: match budget spent, call again with offset={offset + index}')
                break
            try:
                config = future.result(timeout=max(0.0, deadline - time.monotonic()))
            except FutureTimeout:
                notes.append(f'stopped before {fullname}: time budget spent, call again with offset={offset + index}')
                break
            except RequestException as e:
                errors.append({'fullname': fullname, 'error': f'{type(e).__name__}: {e}'[:300]})
                config = None
            pending.popleft()
            index += 1
            refill()
            if config is None:
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
    finally:
        for _, future in pending:
            future.cancel()
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


_MAX_COMPACT_DEFAULT = 200
# A compact page stays under this many characters of JSON, so it fits one MCP client result
# (Claude Code spills a result of about 60k characters to a file and inlines one of about 37k).
_COMPACT_PAGE_CHARS = 30_000


def _compact_parameter(param: dict) -> str:
    """One parameter as 'NAME Type=default [choice|choice]', type without the
    ParameterDefinition suffix and a long text default cut to _MAX_COMPACT_DEFAULT chars."""
    line = f'{param.get("name", "")} {param.get("type", "").removesuffix("ParameterDefinition")}'
    default = param.get('defaultValue', '')
    if isinstance(default, str) and len(default) > _MAX_COMPACT_DEFAULT:
        default = default[:_MAX_COMPACT_DEFAULT] + '...'
    if default not in (None, ''):
        line += f'={default}'
    if param.get('choices'):
        line += ' [' + '|'.join(map(str, param['choices'])) + ']'
    return line


def _compact_entries(items: list) -> list[tuple[str, dict]]:
    """Jobs whose parameter lists are identical collapse into one {jobs, parameters} entry, a job
    with a list of its own stays {fullname, parameters}. Folders (parameters None) are left out.
    Each entry comes with its sort key, the first job name."""
    groups: dict[tuple, list[str]] = {}
    for i in items:
        if i.parameters is None:
            continue
        groups.setdefault(tuple(_compact_parameter(p) for p in i.parameters), []).append(i.fullname)
    keyed = []
    for params, names in groups.items():
        names = sorted(names)
        entry = (
            {'fullname': names[0], 'parameters': list(params)}
            if len(names) == 1
            else {'jobs': names, 'parameters': list(params)}
        )
        keyed.append((names[0], entry))
    return keyed


_MAX_INCLUDE_BUILDS = 200
# A fullname_pattern that selects at most this many jobs reads builds job by job, so a narrow
# query does not make the master load the history of every job.
_PER_JOB_BUILDS_MAX = 20
_UPSTREAM_CAUSE = re.compile(r'^upstream project "(.+)" build number (\d+)$')


def _duration(millis: int | None) -> str:
    """A build duration as 2h37m, 43m or 12s."""
    seconds = int((millis or 0) / 1000)
    hours, rest = divmod(seconds, 3600)
    if hours:
        return f'{hours}h{rest // 60:02d}m'
    return f'{rest // 60}m' if rest >= 60 else f'{rest}s'


def _build_line(build: dict) -> str:
    """One build as '#1484 UNSTABLE 2026-10-20T14:31Z 2h37m user JNKPercona'."""
    result = 'RUNNING' if build.get('building') else build.get('result') or 'NONE'
    started = datetime.fromtimestamp((build.get('timestamp') or 0) / 1000, tz=UTC).strftime('%Y-%m-%dT%H:%MZ')
    line = f'#{build.get("number")} {result} {started} {_duration(build.get("duration"))}'
    causes = [_UPSTREAM_CAUSE.sub(r'upstream \1 #\2', c.removeprefix('Started by ')) for c in build.get('causes') or []]
    return f'{line} {"; ".join(causes)}' if causes else line


def _split_entry(entry: dict, key: str, budget: int) -> list[dict]:
    """An entry larger than budget characters of JSON, as consecutive part entries that each fit.
    The list under key is chunked, every other field is repeated in each part."""
    if len(json.dumps(entry, separators=(',', ':'))) <= budget:
        return [entry]
    rest = {k: v for k, v in entry.items() if k not in (key, 'more')}
    base = len(json.dumps({**rest, 'part': 0, key: []}, separators=(',', ':')))
    chunks: list[list] = [[]]
    size = base
    for value in entry[key]:
        n = len(json.dumps(value, separators=(',', ':'))) + 1
        if chunks[-1] and size + n > budget:
            chunks.append([])
            size = base
        chunks[-1].append(value)
        size += n
    parts = [{**rest, 'part': k, key: c} for k, c in enumerate(chunks, 1)]
    if entry.get('more'):
        parts[-1]['more'] = True
    return parts


def _split_keyed(keyed: list[tuple[str, dict]], budget: int) -> list[tuple[tuple[str, int], dict]]:
    """Sort entries by key and split oversized ones, each part keyed (name, part number)."""
    out = []
    for name, entry in sorted(keyed, key=lambda pair: pair[0]):
        list_key = 'builds' if 'builds' in entry else 'jobs' if 'jobs' in entry else 'parameters'
        for part in _split_entry(entry, list_key, budget):
            out.append(((name, part.get('part', 0)), part))
    return out


def _build_entries(items: list, per_job: int, since_days: int | None) -> list[tuple[str, dict]]:
    """One {fullname, builds} entry per job (folders skipped), builds newest first and inside the
    since_days window. The client fetched per_job + 1 builds, so more=True only when an older
    build exists inside the window."""
    cutoff = (time.time() - since_days * 86400) * 1000 if since_days else None
    keyed = []
    for i in items:
        if i.builds is None:
            continue
        builds = i.builds[:per_job]
        kept = [b for b in builds if cutoff is None or (b.get('timestamp') or 0) >= cutoff]
        entry = {'fullname': i.fullname, 'builds': [_build_line(b) for b in kept]}
        if len(i.builds) > per_job and (cutoff is None or (i.builds[per_job].get('timestamp') or 0) >= cutoff):
            entry['more'] = True
        keyed.append((i.fullname, entry))
    return keyed


def _encode_cursor(key: tuple[str, int]) -> str:
    return f'{key[1]}:{key[0]}'


def _decode_cursor(cursor: str) -> tuple[str, int]:
    part, sep, name = cursor.partition(':')
    if not sep or not part.isdigit() or not name:
        msg = f'cursor {cursor!r} is not one this tool returned, pass next_cursor from the previous result'
        raise ValueError(msg)
    return name, int(part)


def _page_after(
    entries: list[tuple[tuple[str, int], dict]], cursor: str | None, budget: int
) -> tuple[list, str | None]:
    """The entries after cursor that fit budget characters of JSON, and the cursor to continue
    from (None when nothing is left). Keys are stable job names, so a job is never repeated or
    skipped when other entries change size between calls."""
    after = _decode_cursor(cursor) if cursor else None
    remaining = [(k, e) for k, e in entries if after is None or k > after]
    page: list[tuple[tuple[str, int], dict]] = []
    size = 2  # the list brackets
    for key, entry in remaining:
        n = len(json.dumps(entry, separators=(',', ':'))) + (1 if page else 0)
        if page and size + n > budget:
            break
        page.append((key, entry))
        size += n
    more = len(page) < len(remaining)
    return [e for _, e in page], _encode_cursor(page[-1][0]) if more and page else None


@mcp.tool(tags=['read'])
def query_items(
    ctx: Context,
    class_pattern: str = None,
    fullname_pattern: str = None,
    color_pattern: str = None,
    label_pattern: str = None,
    folder_depth: int | None = None,
    limit: int = 200,
    include_parameters: bool = False,  # noqa: FBT001, FBT002
    compact_parameters: bool = False,  # noqa: FBT001, FBT002
    include_builds: int = 0,
    builds_since_days: int | None = None,
    cursor: str | None = None,
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
        include_parameters: Also return each job's parameter definitions (name, type,
            defaultValue, description, choices) in the same single Jenkins request. Use this
            instead of get_item_parameters per job when you need parameters for many jobs.
        compact_parameters: With include_parameters, return each parameter as one string,
            "NAME Type=default [choice|choice]" (no descriptions), collapse jobs with identical
            parameter lists into one {jobs, parameters} entry (folders left out), and page the
            result so every response fits one client result. Call again with cursor=next_cursor
            until next_cursor is null.
        include_builds: Also return up to this many most recent builds per job (capped at 200), each
            one string "#number RESULT start duration cause". Use it for build history across many
            jobs instead of get_build_walk per job. Paged like compact_parameters. more=True marks a
            job with older builds past the cap inside the window. A fullname_pattern that matches
            at most 20 jobs reads only those jobs' history.
        builds_since_days: With include_builds, keep only builds started in the last N days (N >= 1).
        cursor: With compact_parameters or include_builds, the next_cursor of the previous result.

    Returns:
        A dict: items (compact: fullname, class, color, label when the job exposes one, and
        parameters when include_parameters is set), total (items found), returned, truncated.
        With compact_parameters, items are {fullname, parameters} or {jobs, parameters} entries
        (a large one split into numbered parts), returned counts the jobs on this page, and
        next_cursor is added. With include_builds, items are {fullname, builds, more?} entries,
        and builds_total counts builds across all pages.
    """
    if include_builds and include_parameters:
        msg = 'pass include_builds or include_parameters, not both (one call each)'
        raise ValueError(msg)
    if builds_since_days is not None and builds_since_days < 1:
        msg = f'builds_since_days must be 1 or more, got {builds_since_days}'
        raise ValueError(msg)
    limit = max(1, min(limit, 2000))
    per_job = max(0, min(include_builds, _MAX_INCLUDE_BUILDS))
    client = jenkins(ctx, master)
    query = {
        'class_pattern': class_pattern,
        'fullname_pattern': fullname_pattern,
        'color_pattern': color_pattern,
        'label_pattern': label_pattern,
        'folder_depth': folder_depth,
    }
    if per_job and fullname_pattern:
        found = client.query_items(**query)
        jobs = [i for i in found[:limit] if not isinstance(getattr(i, 'jobs', None), list)]
        if len(jobs) <= _PER_JOB_BUILDS_MAX:
            for i in jobs:
                i.builds = client.get_job_builds(fullname=i.fullname, count=per_job + 1)
        else:
            found = client.query_items(**query, include_builds=per_job + 1)
    else:
        found = client.query_items(
            **query, include_parameters=include_parameters, include_builds=per_job + 1 if per_job else 0
        )
    if per_job or (include_parameters and compact_parameters):
        keyed = (
            _build_entries(found[:limit], per_job, builds_since_days) if per_job else _compact_entries(found[:limit])
        )
        entries = _split_keyed(keyed, _COMPACT_PAGE_CHARS)
        page, next_cursor = _page_after(entries, cursor, _COMPACT_PAGE_CHARS)
        result = {
            'items': page,
            'total': len(found),
            'returned': sum(len(e['jobs']) if 'jobs' in e else int(e.get('part', 1) == 1) for e in page),
            'truncated': len(found) > limit or next_cursor is not None,
            'next_cursor': next_cursor,
        }
        if per_job:
            result['builds_total'] = sum(len(e['builds']) for _, e in entries)
        return result
    items = []
    for i in found:
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
    For many jobs at once use query_items(include_parameters=True), one request for all of them.

    Args:
        fullname: The fullname of the item

    Returns:
        A list of parameter definitions, each with name, type, defaultValue, description, and
        choices (for choice parameters).
    """
    return jenkins(ctx, master).get_item_parameters(fullname=fullname)
