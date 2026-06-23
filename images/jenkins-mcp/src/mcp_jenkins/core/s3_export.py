"""S3 export helper: stream a Jenkins log/artifact to a private bucket and presign a download.

Config comes from env (no settings module here, matching core/fleet.py's getenv + lru_cache style):
MCP_S3_BUCKET (required), MCP_AWS_REGION (default us-east-1), MCP_S3_PRESIGN_TTL (seconds, 1..21600).
Uploads stream response.raw straight to S3 single-threaded (bounded memory, correct for a
non-seekable stream) and verify the stored size before presigning, so a truncated upstream never
yields a URL to a partial object. boto3 is imported lazily so it loads only on the export path.
"""

import os
import posixpath
import re
import uuid
from functools import lru_cache
from typing import TYPE_CHECKING

from requests import Response

if TYPE_CHECKING:
    from botocore.client import BaseClient

_MAX_TTL = 21600  # 6h ceiling; see docs/adr/0039


@lru_cache(maxsize=1)
def _bucket() -> str:
    bucket = os.getenv('MCP_S3_BUCKET')
    if not bucket:
        msg = 'MCP_S3_BUCKET is not set; the export tools require an S3 bucket.'
        raise RuntimeError(msg)
    return bucket


@lru_cache(maxsize=1)
def _region() -> str:
    return os.getenv('MCP_AWS_REGION', 'us-east-1')


@lru_cache(maxsize=1)
def _presign_ttl() -> int:
    raw = os.getenv('MCP_S3_PRESIGN_TTL', str(_MAX_TTL))
    try:
        ttl = int(raw)
    except ValueError as e:
        msg = f'MCP_S3_PRESIGN_TTL must be a positive integer, got {raw!r}'
        raise ValueError(msg) from e
    return max(1, min(ttl, _MAX_TTL))


@lru_cache(maxsize=1)
def _s3_client() -> 'BaseClient':
    import boto3
    from botocore.config import Config

    return boto3.client(
        's3',
        region_name=_region(),
        config=Config(signature_version='s3v4', retries={'max_attempts': 0}),
    )


def _slug(value: str) -> str:
    """Reduce an arbitrary string to a key-safe slug (no path separators)."""
    return re.sub(r'[^A-Za-z0-9._-]', '-', value)[:80] or 'x'


def safe_filename(name: str) -> str:
    """Sanitize a download filename to a conservative allowlist (no Content-Disposition injection)."""
    base = posixpath.basename(name.replace('\\', '/'))
    base = re.sub(r'[^A-Za-z0-9._-]', '_', base)
    return base or 'artifact.bin'


def build_key(kind: str, master: str, fullname: str, number: int, filename: str) -> str:
    """Build an unguessable S3 key: the uuid4 segment defeats enumeration, the suffix stays legible."""
    return f'exports/{kind}/{_slug(master)}/{_slug(fullname)}/{number}/{uuid.uuid4().hex}/{filename}'


class _CountingReader:
    """Wrap a file-like, counting bytes read so the upload can be size-verified against S3."""

    def __init__(self, raw: object) -> None:
        self._raw = raw
        self.total = 0

    def read(self, amt: int | None = None) -> bytes:
        chunk = self._raw.read(amt)
        self.total += len(chunk)
        return chunk


def upload_stream(response: Response, *, key: str, content_type: str, filename: str) -> int:
    """Stream response.raw to S3 (bounded memory) and return the stored object size in bytes.

    Single-threaded transfer keeps a non-seekable stream correct and peak memory near one part. The
    stored size is checked against the bytes read and the upstream Content-Length (when present and
    not transfer-encoded); on a mismatch the object is deleted and an error is raised, so a truncated
    upload is never presigned.
    """
    from boto3.s3.transfer import TransferConfig

    client = _s3_client()
    bucket = _bucket()
    reader = _CountingReader(response.raw)
    extra = {
        'ContentType': content_type,
        'ContentDisposition': f'attachment; filename="{filename}"',
    }
    client.upload_fileobj(reader, bucket, key, ExtraArgs=extra, Config=TransferConfig(use_threads=False))

    stored = client.head_object(Bucket=bucket, Key=key)['ContentLength']
    declared = response.headers.get('Content-Length')
    encoded = response.headers.get('Content-Encoding')
    complete = declared is None or encoded is not None or int(declared) == reader.total
    if stored != reader.total or not complete:
        client.delete_object(Bucket=bucket, Key=key)
        msg = f'export integrity check failed for {key}: read {reader.total}, stored {stored}, declared {declared}'
        raise RuntimeError(msg)
    return stored


def upload_bytes(data: bytes, *, key: str, content_type: str, filename: str) -> int:
    """Upload an in-memory payload to S3 (for an already-read, size-bounded artifact member).

    Used by the archive-extract path, where the member has been read into memory under a hard size
    cap (so this never buffers an unbounded object). Returns the stored size in bytes.
    """
    client = _s3_client()
    client.put_object(
        Bucket=_bucket(),
        Key=key,
        Body=data,
        ContentType=content_type,
        ContentDisposition=f'attachment; filename="{filename}"',
    )
    return len(data)


def presign_response(*, key: str, size: int, content_type: str, filename: str) -> dict:
    """Presign a time-limited GET and return the download metadata. The url is a bearer capability."""
    bucket = _bucket()
    ttl = _presign_ttl()
    url = _s3_client().generate_presigned_url(
        'get_object',
        Params={'Bucket': bucket, 'Key': key},
        ExpiresIn=ttl,
    )
    return {
        'url': url,
        'bucket': bucket,
        'key': key,
        'size_bytes': size,
        'content_type': content_type,
        'filename': filename,
        'requested_ttl_seconds': ttl,
        'expiry_caveat': (
            'The url is a bearer capability: anyone with it can download with no auth until it expires. '
            'It may expire earlier than requested_ttl_seconds if the pod credentials rotate. '
            'The object auto-deletes after 7 days.'
        ),
    }
