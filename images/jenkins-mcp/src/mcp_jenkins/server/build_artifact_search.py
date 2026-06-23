"""Search and extract WITHIN build artifacts: grep a log, list a tarball, pull one member.

Read tools (any authenticated user). Everything STREAMS the artifact (reuses
rest_client.stream_build_artifact) and is HARD-CAPPED at READ TIME, so a call can never buffer a
whole file, walk an unbounded log, or unpack a decompression bomb. Tarballs are gunzipped through a
byte-limited reader and read in streaming mode (`r|`), never written to disk; member paths and types
are validated so a crafted archive cannot escape, exhaust memory, or hang. The motivating case: MTR
per-test logs live inside work/results/<job>-test-mtr_logs-N.tar.gz.
"""

import base64
import fnmatch
import gzip
import re
import tarfile
import zlib
from collections import deque
from collections.abc import Callable, Iterator
from typing import TYPE_CHECKING, BinaryIO

from fastmcp import Context

from mcp_jenkins.core import s3_export
from mcp_jenkins.core.lifespan import MasterArg, _selected_master, jenkins
from mcp_jenkins.server import mcp
from mcp_jenkins.server.build_export import _emit_export_audit, _resolve_number, _validate_relative_path

if TYPE_CHECKING:
    from requests import Response

# Every artifact read is bounded AT READ TIME so one call can never buffer a whole file or scan forever.
_CHUNK = 65536  # read granularity for the chunked line reader and limited reader
_MAX_BYTES_SCANNED = 64 * 1024 * 1024  # grep: stop after this many (decompressed) bytes
_MAX_LINE_LEN = 8192  # grep: cap a single line at read time (a no-newline blob never buffers past this)
_MAX_MATCHES = 200  # grep: hard ceiling on returned matches
_MAX_CONTEXT_LINES = 10  # grep: hard ceiling on context_lines
_MAX_PATTERN_LEN = 2000  # grep: reject longer regex patterns (ReDoS surface)
_MAX_ENTRIES = 2000  # list: hard ceiling on returned members
_MAX_MEMBERS_SCANNED = 50000  # list/grep/extract: hard ceiling on members examined
_MAX_DECOMPRESSED_TOTAL = 256 * 1024 * 1024  # any tar op: cumulative gunzipped bytes (bomb guard)
_MAX_MEMBER_BYTES = 25 * 1024 * 1024  # extract: largest single member we will read
_MAX_INLINE_BYTES = 256 * 1024  # extract: inline up to this, else S3


class _ArchiveLimitError(Exception):
    """Raised when a tar op hits a member-count or decompressed-bytes cap (bomb guard)."""


class _LimitedReader:
    """Wrap a binary stream so total bytes READ can never exceed a cap (decompression-bomb guard).

    Sits ABOVE the gunzip layer (it wraps the decompressed stream that feeds tarfile), so the cap
    bounds member content AND the PAX/GNU/sparse metadata tarfile reads internally.
    """

    def __init__(self, raw: BinaryIO, limit: int) -> None:
        self._raw = raw
        self._limit = limit
        self._read = 0

    def read(self, size: int = -1) -> bytes:
        chunk = self._raw.read(size)
        self._read += len(chunk)
        if self._read > self._limit:
            msg = f'archive exceeds the {self._limit}-byte decompression limit'
            raise _ArchiveLimitError(msg)
        return chunk


def _open_tar(response: 'Response') -> tarfile.TarFile:
    """Open the artifact as a streaming, decompressed-byte-bounded tar (gunzip done here, not by tarfile)."""
    gz = gzip.GzipFile(fileobj=response.raw, mode='rb')
    return tarfile.open(fileobj=_LimitedReader(gz, _MAX_DECOMPRESSED_TOTAL), mode='r|')


def _iter_members(tf: tarfile.TarFile) -> Iterator[tarfile.TarInfo]:
    """Yield members while bounding count (the decompressed-bytes cap lives in _LimitedReader)."""
    count = 0
    for info in tf:
        count += 1
        if count > _MAX_MEMBERS_SCANNED:
            msg = f'archive has more than {_MAX_MEMBERS_SCANNED} members'
            raise _ArchiveLimitError(msg)
        tf.members = []  # bound the TarInfo cache in stream mode
        yield info


def _is_real_file(info: tarfile.TarInfo) -> bool:
    """A plain regular file we will read: excludes symlink/hardlink/device/fifo/dir and sparse files."""
    return info.isfile() and not info.issparse()


def _is_targz(path: str) -> bool:
    return path.endswith(('.tar.gz', '.tgz'))


def _require_targz(path: str) -> None:
    if not _is_targz(path):
        msg = f'relative_path must be a .tar.gz/.tgz archive: {path!r}'
        raise ValueError(msg)


def _safe_member_name(member: str) -> str:
    """Validate a tar member path: reject absolute, traversal, backslash, and control chars."""
    if not isinstance(member, str) or not member.strip():
        msg = 'member must be a non-empty string'
        raise ValueError(msg)
    if member.startswith('/') or '\\' in member or '..' in member.split('/'):
        msg = f'unsafe member path (absolute or traversal): {member!r}'
        raise ValueError(msg)
    if any(ord(c) < 0x20 for c in member):
        msg = f'member must not contain control characters: {member!r}'
        raise ValueError(msg)
    return member


def _is_safe_name(name: str) -> bool:
    try:
        _safe_member_name(name)
    except ValueError:
        return False
    return True


def _build_matcher(pattern: str, literal: bool, ignore_case: bool) -> Callable[[str], bool]:  # noqa: FBT001
    """Build a per-line predicate. Literal substring by default; regex is opt-in and length-capped."""
    if not isinstance(pattern, str) or pattern == '':
        msg = 'pattern must be a non-empty string'
        raise ValueError(msg)
    if len(pattern) > _MAX_PATTERN_LEN:
        msg = f'pattern too long ({len(pattern)} > {_MAX_PATTERN_LEN} chars)'
        raise ValueError(msg)
    if literal:
        if ignore_case:
            needle = pattern.lower()
            return lambda line: needle in line.lower()
        return lambda line: pattern in line
    try:
        rx = re.compile(pattern, re.IGNORECASE if ignore_case else 0)
    except re.error as e:
        msg = f'invalid regex pattern: {e}'
        raise ValueError(msg) from e
    return lambda line: rx.search(line) is not None


def _bounded_line_reader(read: Callable[[int], bytes], holder: dict) -> Iterator[tuple[int, str]]:
    """Yield (line_no, line) from a byte reader, bounded in MEMORY and total bytes at read time.

    The pending line buffer is capped at _MAX_LINE_LEN: a line longer than that is emitted truncated
    and the rest discarded until the next newline, so a no-newline blob never buffers unbounded. The
    scan stops at _MAX_BYTES_SCANNED (holder['truncated'] set). Decodes UTF-8 with replacement.
    """
    n = 0
    buf = bytearray()
    eating = False  # discarding the tail of an over-length line until the next newline
    while holder['scanned'] < _MAX_BYTES_SCANNED:
        chunk = read(min(_CHUNK, _MAX_BYTES_SCANNED - holder['scanned']))
        if not chunk:
            break
        holder['scanned'] += len(chunk)
        buf.extend(chunk)
        while True:
            nl = buf.find(b'\n')
            if nl == -1:
                if eating:
                    buf.clear()
                elif len(buf) > _MAX_LINE_LEN:
                    n += 1
                    yield n, bytes(buf[:_MAX_LINE_LEN]).decode('utf-8', 'replace')
                    buf.clear()
                    eating = True
                break
            segment = bytes(buf[:nl])
            del buf[: nl + 1]
            if eating:
                eating = False
                continue
            if len(segment) > _MAX_LINE_LEN:
                segment = segment[:_MAX_LINE_LEN]
            n += 1
            yield n, segment.decode('utf-8', 'replace')
    else:
        holder['truncated'] = True
    if buf and not eating:
        n += 1
        yield n, bytes(buf[:_MAX_LINE_LEN]).decode('utf-8', 'replace')


def _grep_stream(
    lines: Iterator[tuple[int, str]], *, matcher: Callable[[str], bool], context_lines: int, max_matches: int
) -> tuple[list[dict], bool]:
    """Collect matches with before/after context from a streaming line source; cap at max_matches."""
    before: deque[str] = deque(maxlen=context_lines)
    matches: list[dict] = []
    pending: list[dict] = []
    limit_hit = False
    for _line_no, line in lines:
        for m in pending:
            m['after'].append(line)
        pending = [m for m in pending if len(m['after']) < context_lines]
        if matcher(line):
            if len(matches) >= max_matches:
                limit_hit = True
                break
            entry = {'line_no': _line_no, 'line': line, 'before': list(before), 'after': []}
            matches.append(entry)
            if context_lines:
                pending.append(entry)
        before.append(line)
    return matches, limit_hit


@mcp.tool(tags=['read'])
async def grep_build_artifact(
    ctx: Context,
    fullname: str,
    relative_path: str,
    pattern: str,
    number: int | None = None,
    archive_member: str | None = None,
    ignore_case: bool = False,  # noqa: FBT001, FBT002
    literal: bool = True,  # noqa: FBT001, FBT002
    context_lines: int = 3,
    max_matches: int = 50,
    master: MasterArg = None,
) -> dict:
    """Grep INSIDE a build artifact without downloading the whole file (streamed, capped).

    Returns only the matching lines (with context), so you can pull the failure lines out of a large
    log. If archive_member is set, greps that member of a `.tar.gz` (e.g. an MTR per-test log inside
    work/results/<job>-test-mtr_logs-N.tar.gz). For the whole file use export_build_artifact.

    Args:
        fullname: The fullname of the job
        relative_path: Artifact path (e.g. work/results/foo-mtr_logs-5.tar.gz or a plain .log)
        pattern: Substring (default) or regex (set literal=False) matched per line
        number: Build number, if None the last build
        archive_member: If set, grep this member inside the .tar.gz at relative_path
        ignore_case: Case-insensitive match
        literal: Treat pattern as a literal substring (default). Set False for regex (bounded input,
            but Python re has no timeout, so a pathological regex can still burn CPU on a long line).
        context_lines: Lines of context to keep before/after each match (capped at 10)
        max_matches: Max matches to return (capped at 200)

    Returns:
        A dict: matches [{line_no, line, before, after}], match_count, bytes_scanned, truncated
        (byte cap hit), limit_hit (match cap hit), plus the echoed query.
    """
    safe_path = _validate_relative_path(relative_path)
    context_lines = max(0, min(context_lines, _MAX_CONTEXT_LINES))
    max_matches = max(1, min(max_matches, _MAX_MATCHES))
    matcher = _build_matcher(pattern, literal, ignore_case)
    safe_member = None
    if archive_member is not None:
        _require_targz(safe_path)
        safe_member = _safe_member_name(archive_member)

    client = jenkins(ctx, master)
    number = _resolve_number(client, fullname, number)
    holder = {'scanned': 0, 'truncated': False}
    with client.stream_build_artifact(fullname=fullname, number=number, relative_path=safe_path) as response:
        if safe_member is None:
            lines = _bounded_line_reader(response.raw.read, holder)
            matches, limit_hit = _grep_stream(
                lines, matcher=matcher, context_lines=context_lines, max_matches=max_matches
            )
        else:
            matches, limit_hit = _grep_member(
                response, safe_member, holder, matcher=matcher, context_lines=context_lines, max_matches=max_matches
            )

    return {
        'relative_path': safe_path,
        'archive_member': safe_member,
        'pattern': pattern,
        'literal': literal,
        'match_count': len(matches),
        'matches': matches,
        'bytes_scanned': holder['scanned'],
        'truncated': holder['truncated'],
        'limit_hit': limit_hit,
    }


def _grep_member(
    response: 'Response',
    member: str,
    holder: dict,
    *,
    matcher: Callable[[str], bool],
    context_lines: int,
    max_matches: int,
) -> tuple[list[dict], bool]:
    """Find a member (capped), validate it, and grep its lines through the bounded reader."""
    try:
        tf = _open_tar(response)
        for info in _iter_members(tf):
            if info.name != member:
                continue
            if not _is_real_file(info):
                msg = f'member is not a regular file: {member!r}'
                raise ValueError(msg)
            if info.size > _MAX_MEMBER_BYTES:
                msg = f'member too large to read: {info.size} > {_MAX_MEMBER_BYTES} bytes'
                raise ValueError(msg)
            fobj = tf.extractfile(info)
            if fobj is None:
                msg = f'cannot read member: {member!r}'
                raise ValueError(msg)
            lines = _bounded_line_reader(fobj.read, holder)
            return _grep_stream(lines, matcher=matcher, context_lines=context_lines, max_matches=max_matches)
    except _ArchiveLimitError as e:
        raise ValueError(str(e)) from None
    except (tarfile.TarError, EOFError, OSError, zlib.error) as e:
        msg = 'invalid or unsupported tar.gz archive'
        raise ValueError(msg) from e
    msg = f'member not found in archive: {member!r}'
    raise ValueError(msg)


@mcp.tool(tags=['read'])
async def list_archive_artifact(
    ctx: Context,
    fullname: str,
    relative_path: str,
    glob: str = '**',
    number: int | None = None,
    max_entries: int = 500,
    master: MasterArg = None,
) -> dict:
    """List the files inside a `.tar.gz` build artifact (streamed, capped).

    Use this to discover member paths (e.g. which per-test logs are in an MTR logs tarball) before
    extract_archive_artifact / grep_build_artifact. Only safe, regular-file members are returned.

    Args:
        fullname: The fullname of the job
        relative_path: Artifact path of the .tar.gz/.tgz
        glob: fnmatch-style filter on the member path (`*` spans `/`); default `**` = all
        number: Build number, if None the last build
        max_entries: Max members to return (capped at 2000)

    Returns:
        A dict: entries [{name, size}], total_listed, truncated (more members exist than returned).
    """
    safe_path = _validate_relative_path(relative_path)
    _require_targz(safe_path)
    max_entries = max(1, min(max_entries, _MAX_ENTRIES))
    match = (lambda _name: True) if glob in ('**', '*', '') else (lambda name: fnmatch.fnmatch(name, glob))

    client = jenkins(ctx, master)
    number = _resolve_number(client, fullname, number)
    entries: list[dict] = []
    truncated = False
    with client.stream_build_artifact(fullname=fullname, number=number, relative_path=safe_path) as response:
        try:
            tf = _open_tar(response)
            for info in _iter_members(tf):
                if not _is_real_file(info) or not _is_safe_name(info.name) or not match(info.name):
                    continue
                if len(entries) >= max_entries:
                    truncated = True
                    break
                entries.append({'name': info.name, 'size': info.size})
        except _ArchiveLimitError:
            truncated = True  # stopped at a member-count / decompression cap; report partial
        except (tarfile.TarError, EOFError, OSError, zlib.error) as e:
            msg = 'invalid or unsupported tar.gz archive'
            raise ValueError(msg) from e

    return {
        'relative_path': safe_path,
        'glob': glob,
        'entries': entries,
        'total_listed': len(entries),
        'truncated': truncated,
    }


@mcp.tool(tags=['read'])
async def extract_archive_artifact(
    ctx: Context,
    fullname: str,
    relative_path: str,
    member: str,
    number: int | None = None,
    max_inline_bytes: int = _MAX_INLINE_BYTES,
    master: MasterArg = None,
) -> dict:
    """Extract ONE file from a `.tar.gz` build artifact (streamed, size-capped).

    Small members come back inline (utf-8, or base64 for binary); larger ones are streamed to S3 and
    returned as a presigned URL (like export_build_artifact). Discover member names with
    list_archive_artifact.

    Args:
        fullname: The fullname of the job
        relative_path: Artifact path of the .tar.gz/.tgz
        member: Exact member path to extract (from list_archive_artifact)
        number: Build number, if None the last build
        max_inline_bytes: Return inline up to this size (capped at 256 KiB); larger -> S3 URL

    Returns:
        Inline: {member, size_bytes, encoding ('utf-8'|'base64'), content}.
        Large:  {member, size_bytes, url, bucket, key, ...} (the url is a bearer capability).
    """
    safe_path = _validate_relative_path(relative_path)
    _require_targz(safe_path)
    safe_member = _safe_member_name(member)
    max_inline_bytes = max(1, min(max_inline_bytes, _MAX_INLINE_BYTES))

    client = jenkins(ctx, master)
    number = _resolve_number(client, fullname, number)
    data: bytes | None = None
    with client.stream_build_artifact(fullname=fullname, number=number, relative_path=safe_path) as response:
        try:
            tf = _open_tar(response)
            for info in _iter_members(tf):
                if info.name != safe_member:
                    continue
                if not _is_real_file(info):
                    msg = f'member is not a regular file: {safe_member!r}'
                    raise ValueError(msg)
                if info.size > _MAX_MEMBER_BYTES:
                    msg = f'member too large to extract: {info.size} > {_MAX_MEMBER_BYTES} bytes (use the Jenkins UI)'
                    raise ValueError(msg)
                fobj = tf.extractfile(info)
                if fobj is None:
                    msg = f'cannot read member: {safe_member!r}'
                    raise ValueError(msg)
                data = fobj.read(info.size)
                break
        except _ArchiveLimitError as e:
            raise ValueError(str(e)) from None
        except (tarfile.TarError, EOFError, OSError, zlib.error) as e:
            msg = 'invalid or unsupported tar.gz archive'
            raise ValueError(msg) from e
    if data is None:
        msg = f'member not found in archive: {safe_member!r}'
        raise ValueError(msg)

    if len(data) <= max_inline_bytes:
        try:
            return {
                'member': safe_member,
                'size_bytes': len(data),
                'encoding': 'utf-8',
                'content': data.decode('utf-8'),
            }
        except UnicodeDecodeError:
            return {
                'member': safe_member,
                'size_bytes': len(data),
                'encoding': 'base64',
                'content': base64.b64encode(data).decode('ascii'),
            }

    selected = _selected_master(ctx, master)
    filename = s3_export.safe_filename(safe_member)
    content_type = 'application/octet-stream'
    key = s3_export.build_key('archive', selected, fullname, number, filename)
    size = s3_export.upload_bytes(data, key=key, content_type=content_type, filename=filename)
    _emit_export_audit(
        tool='extract_archive_artifact', master=selected, fullname=fullname, number=number, key=key, size=size
    )
    return {
        'member': safe_member,
        **s3_export.presign_response(key=key, size=size, content_type=content_type, filename=filename),
    }
