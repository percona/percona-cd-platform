"""Export tools: stream a build log or artifact to S3 and return a signed download URL.

These are read tools (any authenticated user): the Jenkins read is the same one the inline tools do,
the payload is just moved out of band so large logs/artifacts never traverse this response. The
returned url is a short-lived presigned GET (a bearer capability); the bytes never come back inline.
Each successful export emits an audit line carrying the S3 key (never the url), since the audit
middleware logs arguments, not return values.
"""

import json
import mimetypes
import posixpath
import re
import urllib.parse
import uuid
from typing import TYPE_CHECKING

from fastmcp import Context
from fastmcp.server.dependencies import get_access_token
from loguru import logger

from mcp_jenkins.core import s3_export
from mcp_jenkins.core.lifespan import MasterArg, _selected_master, jenkins
from mcp_jenkins.server import mcp

if TYPE_CHECKING:
    from mcp_jenkins.jenkins import Jenkins


def _resolve_number(client: 'Jenkins', fullname: str, number: int | None) -> int:
    """Resolve a build number, defaulting to the job's last build; raise if the job has none."""
    if number is not None:
        return number
    item = client.get_item(fullname=fullname, depth=1)
    last = getattr(item, 'lastBuild', None)
    resolved = getattr(last, 'number', None) if last else None
    if resolved is None:
        msg = f'No build found for job: {fullname}'
        raise ValueError(msg)
    return resolved


def _validate_relative_path(relative_path: str) -> str:
    """Reject traversal/encoded/absolute artifact paths before they reach the S3 key or Jenkins URL.

    posixpath.normpath alone is insufficient (it accepts percent-encoded traversal like %2e%2e), so
    this also rejects percent-encoding, backslashes, drive letters, control chars, and any non
    allowlisted character, then returns the canonical path.
    """
    if not isinstance(relative_path, str) or not relative_path.strip():
        msg = 'relative_path must be a non-empty string'
        raise ValueError(msg)
    if urllib.parse.unquote(relative_path) != relative_path:
        msg = f'relative_path must not be percent-encoded: {relative_path!r}'
        raise ValueError(msg)
    if '\\' in relative_path or re.match(r'[A-Za-z]:', relative_path):
        msg = f'relative_path must not contain backslashes or drive letters: {relative_path!r}'
        raise ValueError(msg)
    if any(ord(c) < 0x20 for c in relative_path):
        msg = f'relative_path must not contain control characters: {relative_path!r}'
        raise ValueError(msg)
    norm = posixpath.normpath(relative_path)
    if norm.startswith('/') or norm == '.' or '..' in norm.split('/'):
        msg = f'unsafe relative_path (traversal): {relative_path!r}'
        raise ValueError(msg)
    if not re.fullmatch(r'[A-Za-z0-9._/-]+', norm):
        msg = f'relative_path has invalid characters: {relative_path!r}'
        raise ValueError(msg)
    return norm


def _emit_export_audit(*, tool: str, master: str, fullname: str, number: int, key: str, size: int) -> None:
    """Emit an audit=True JSON line recording the export's S3 key, never the presigned url.

    The audit middleware logs call arguments, not return values, so it does not capture the key. This
    line lands in the same Loki sink, correlatable by sub + tool, recording where the export went.
    """
    try:
        token = get_access_token()
        sub = (token.claims or {}).get('sub') if token else 'anonymous'
    except Exception:  # noqa: BLE001
        sub = 'anonymous'
    record = {
        'event': 'mcp_export',
        'request_id': uuid.uuid4().hex,
        'sub': sub,
        'tool': tool,
        'master': master,
        'fullname': fullname,
        'number': number,
        'key': key,
        'size_bytes': size,
    }
    logger.bind(audit=True).info(json.dumps(record, default=str))


@mcp.tool(tags=['read'])
async def export_build_log(ctx: Context, fullname: str, number: int | None = None, master: MasterArg = None) -> dict:
    """Export a build's FULL console log to S3 and return a time-limited signed download URL.

    Streams the whole console log server-side straight to a private S3 bucket (it never passes
    through this response), then returns a presigned GET url plus metadata. Prefer this over
    get_build_console_output for large logs you want to download.

    WARNING: the returned `url` is a sensitive bearer capability. Anyone who has it can download the
    log with no authentication until it expires (up to 6h, sooner if the pod credentials rotate). The
    object auto-deletes after 7 days. The url, not the bytes, is what comes back.

    Args:
        fullname: The fullname of the job
        number: The number of the build, if None, the last build

    Returns:
        A dict: url, bucket, key, size_bytes, content_type, filename, requested_ttl_seconds, expiry_caveat
    """
    client = jenkins(ctx, master)
    number = _resolve_number(client, fullname, number)
    selected = _selected_master(ctx, master)
    job_name = fullname.split('/')[-1]
    filename = s3_export.safe_filename(f'{job_name}-{number}-console.txt')
    content_type = 'text/plain; charset=utf-8'
    key = s3_export.build_key('log', selected, fullname, number, filename)
    with client.stream_build_console_output(fullname=fullname, number=number) as response:
        size = s3_export.upload_stream(response, key=key, content_type=content_type, filename=filename)
    _emit_export_audit(tool='export_build_log', master=selected, fullname=fullname, number=number, key=key, size=size)
    return s3_export.presign_response(key=key, size=size, content_type=content_type, filename=filename)


@mcp.tool(tags=['read'])
async def export_build_artifact(
    ctx: Context, fullname: str, relative_path: str, number: int | None = None, master: MasterArg = None
) -> dict:
    """Export a build artifact to S3 and return a time-limited signed download URL.

    Streams the artifact server-side straight to a private S3 bucket (it never passes through this
    response), then returns a presigned GET url plus metadata. Prefer this over get_build_artifact
    for large artifacts you want to download.

    WARNING: the returned `url` is a sensitive bearer capability. Anyone who has it can download the
    artifact with no authentication until it expires (up to 6h, sooner if the pod credentials
    rotate). The object auto-deletes after 7 days. The url, not the bytes, is what comes back.

    Args:
        fullname: The fullname of the job
        relative_path: The relative path of the artifact (e.g. dist/app.tar.gz)
        number: The number of the build, if None, the last build

    Returns:
        A dict: url, bucket, key, size_bytes, content_type, filename, requested_ttl_seconds, expiry_caveat
    """
    safe_path = _validate_relative_path(relative_path)
    client = jenkins(ctx, master)
    number = _resolve_number(client, fullname, number)
    selected = _selected_master(ctx, master)
    filename = s3_export.safe_filename(safe_path)
    content_type = mimetypes.guess_type(filename)[0] or 'application/octet-stream'
    key = s3_export.build_key('artifact', selected, fullname, number, filename)
    with client.stream_build_artifact(fullname=fullname, number=number, relative_path=safe_path) as response:
        size = s3_export.upload_stream(response, key=key, content_type=content_type, filename=filename)
    _emit_export_audit(
        tool='export_build_artifact', master=selected, fullname=fullname, number=number, key=key, size=size
    )
    return s3_export.presign_response(key=key, size=size, content_type=content_type, filename=filename)
