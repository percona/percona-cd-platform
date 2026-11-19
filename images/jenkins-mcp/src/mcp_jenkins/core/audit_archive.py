"""Archive of the audit records of write calls in S3, kept longer than Loki keeps the audit lines.

The audit middleware hands over the JSON line it logs for an operate, manage or write call. A small
pool of its own stores that line as one S3 object, so the S3 call runs neither on the event loop nor
in the worker threads of the tools, and the tool result never waits for it. Every failure ends in the
counter mcp_audit_archive_failures and a log line, never in the caller. Off unless MCP_AUDIT_S3_BUCKET
is set. The region comes from MCP_AWS_REGION (default us-east-1), as for the export bucket.
"""

import os
import re
import threading
import uuid
from concurrent.futures import Future, ThreadPoolExecutor
from datetime import UTC, datetime
from functools import lru_cache
from typing import TYPE_CHECKING

from loguru import logger
from prometheus_client import Counter

if TYPE_CHECKING:
    from botocore.client import BaseClient

BUCKET_ENV = 'MCP_AUDIT_S3_BUCKET'
PREFIX = 'audit/write'
# Records waiting for S3. Beyond this a record is dropped and counted, so a dead sink cannot grow memory.
_MAX_PENDING = 256
_CONNECT_TIMEOUT_S = 2
_READ_TIMEOUT_S = 3
# Hex digits of the random part of a key, the length of a uuid4 hex.
_RAND_LEN = 32

_STORED = Counter('mcp_audit_archive_stored', 'Write-call audit records stored in S3.')
_FAILURES = Counter('mcp_audit_archive_failures', 'Write-call audit records that did not reach S3.', ['reason'])
for _reason in ('error', 'timeout', 'backlog'):
    _FAILURES.labels(reason=_reason)

_executor = ThreadPoolExecutor(max_workers=2, thread_name_prefix='audit-archive')
_pending_lock = threading.Lock()
_pending = 0


@lru_cache(maxsize=1)
def bucket() -> str | None:
    """The archive bucket, or None when the sink is off."""
    return os.getenv(BUCKET_ENV, '').strip() or None


@lru_cache(maxsize=1)
def _client() -> 'BaseClient':
    import boto3
    from botocore.config import Config

    return boto3.client(
        's3',
        region_name=os.getenv('MCP_AWS_REGION', 'us-east-1'),
        config=Config(
            signature_version='s3v4',
            connect_timeout=_CONNECT_TIMEOUT_S,
            read_timeout=_READ_TIMEOUT_S,
            retries={'total_max_attempts': 2, 'mode': 'standard'},
        ),
    )


def object_key(ts: datetime, request_id: str) -> str:
    """Time first, then the random request id: keys sort by time, never collide and name no user."""
    ts = ts.astimezone(UTC) if ts.tzinfo else ts.replace(tzinfo=UTC)
    rand = re.sub(r'[^0-9a-f]', '', request_id.lower())[:_RAND_LEN]
    if len(rand) != _RAND_LEN:
        rand = uuid.uuid4().hex
    return f'{PREFIX}/{ts:%Y/%m/%d}/{ts:%Y%m%dT%H%M%S.%fZ}-{rand}.json'


def _is_timeout(error: Exception) -> bool:
    from botocore.exceptions import ConnectTimeoutError, ReadTimeoutError

    return isinstance(error, ConnectTimeoutError | ReadTimeoutError | TimeoutError)


def _release() -> None:
    global _pending  # noqa: PLW0603
    with _pending_lock:
        _pending -= 1


def _put(target: str, key: str, body: bytes) -> None:
    try:
        # SHA256 makes the put carry a checksum header, which Object Lock requires. If-None-Match
        # refuses to replace an object that already holds the key.
        _client().put_object(
            Bucket=target,
            Key=key,
            Body=body,
            ContentType='application/json',
            ChecksumAlgorithm='SHA256',
            IfNoneMatch='*',
        )
        _STORED.inc()
    except Exception as e:  # noqa: BLE001
        reason = 'timeout' if _is_timeout(e) else 'error'
        _FAILURES.labels(reason=reason).inc()
        logger.warning('audit archive: {} storing {}: {}', reason, key, type(e).__name__)
    finally:
        _release()


def archive(line: str, ts: datetime, request_id: str) -> Future | None:
    """Queue one audit line for S3 and return at once. Never raises. None when nothing was queued."""
    global _pending  # noqa: PLW0603
    try:
        target = bucket()
        if target is None:
            return None
        key = object_key(ts, request_id)
        with _pending_lock:
            if _pending >= _MAX_PENDING:
                _FAILURES.labels(reason='backlog').inc()
                logger.warning('audit archive: backlog full, dropped {}', key)
                return None
            _pending += 1
        try:
            return _executor.submit(_put, target, key, line.encode())
        except BaseException:
            _release()
            raise
    except Exception as e:  # noqa: BLE001
        _FAILURES.labels(reason='error').inc()
        logger.warning('audit archive: could not queue a record: {}', type(e).__name__)
        return None
