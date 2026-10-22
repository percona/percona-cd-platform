import asyncio
import functools
import time
from collections.abc import Callable
from typing import TYPE_CHECKING, TypeVar

import anyio
from fastmcp import Context
from requests.exceptions import HTTPError

from mcp_jenkins.core.lifespan import MasterArg, jenkins
from mcp_jenkins.server import mcp

if TYPE_CHECKING:
    from mcp_jenkins.jenkins import Jenkins

_T = TypeVar('_T')


@mcp.tool(tags=['read'])
async def get_all_queue_items(ctx: Context, master: MasterArg = None) -> list[dict]:
    """Get all items in Jenkins queue

    Returns:
        A list of all items in the Jenkins queue
    """
    return [item.model_dump(exclude_none=True, exclude={'task'}) for item in jenkins(ctx, master).get_queue().items]


@mcp.tool(tags=['read'])
async def get_queue_item(
    ctx: Context, id: int | None = None, number: int | None = None, master: MasterArg = None
) -> dict:
    """Get a specific item in Jenkins queue by id

    Args:
        id: The id of the queue item (the int build_item and replay_build return)
        number: Alias of id, for callers that name the queue item a number

    Returns:
        The queue item
    """
    if id is None:
        id = number
    if id is None:
        msg = 'get_queue_item needs id (the queue item id that build_item returns)'
        raise ValueError(msg)
    item = jenkins(ctx, master).get_queue_item(id=id, depth=1)
    return item.model_dump(exclude_none=True)


@mcp.tool(tags=['operate'])
async def cancel_queue_item(ctx: Context, id: int, master: MasterArg = None) -> None:
    """Cancel a specific item in Jenkins queue by id

    Args:
        id: The id of the queue item
    """
    jenkins(ctx, master).cancel_queue_item(id=id)


# wait_for_build caps: one call never holds a client longer than a minute; poll every 2 s.
_MAX_WAIT_SECONDS = 60
_POLL_INTERVAL_SECONDS = 2
# After Jenkins forgets a started queue item (about 5 minutes), the build is found by queueId here.
_QUEUE_FALLBACK_HISTORY = 20


async def _blocking(fn: Callable[..., _T], **kwargs: object) -> _T:
    """Run one blocking Jenkins request on a worker thread (contextvars, so the caller's scope, carry over)."""
    return await anyio.to_thread.run_sync(functools.partial(fn, **kwargs))


async def _number_from_history(client: 'Jenkins', fullname: str, queue_id: int) -> int | None:
    builds = await _blocking(client.get_build_history, fullname=fullname, count=_QUEUE_FALLBACK_HISTORY)
    return next((b.number for b in builds if b.queueId == queue_id), None)


@mcp.tool(tags=['read'])
async def wait_for_build(
    ctx: Context,
    fullname: str,
    queue_id: int | None = None,
    number: int | None = None,
    timeout_seconds: int = 30,
    master: MasterArg = None,
) -> dict:
    """Wait for a queued or running build and report where it got to (instead of polling by hand).

    Pass the queue_id that build_item / replay_build returned to learn the build number once it
    starts, or a build number to wait for it to finish. Never raises on timeout: call again with
    the returned number to keep waiting.

    Args:
        fullname: The fullname of the job
        queue_id: The queue item id from build_item or replay_build
        number: A build number (skips the queue phase)
        timeout_seconds: How long to wait in this call (capped at 60)

    Returns:
        A dict: queue_id, number, url, state (queued, running, finished, cancelled, not_found),
        result, building, timed_out, waited_seconds.
    """
    if queue_id is None and number is None:
        msg = 'wait_for_build needs queue_id (from build_item) or number'
        raise ValueError(msg)
    client = jenkins(ctx, master)
    start = time.monotonic()
    deadline = start + max(1, min(timeout_seconds, _MAX_WAIT_SECONDS))
    out: dict = {'queue_id': queue_id, 'number': number, 'url': None, 'result': None, 'building': None}

    def done(state: str, *, timed_out: bool = False) -> dict:
        return {**out, 'state': state, 'timed_out': timed_out, 'waited_seconds': round(time.monotonic() - start, 1)}

    while number is None:
        try:
            item = await _blocking(client.get_queue_item, id=queue_id, depth=1)
        except HTTPError as e:
            if e.response is None or e.response.status_code != 404:
                raise
            number = await _number_from_history(client, fullname, queue_id)
            if number is None:
                return done('not_found')
            break
        if item.cancelled:
            return done('cancelled')
        if item.executable is not None and item.executable.number is not None:
            number = item.executable.number
            break
        if time.monotonic() + _POLL_INTERVAL_SECONDS > deadline:
            return done('queued', timed_out=True)
        await asyncio.sleep(_POLL_INTERVAL_SECONDS)

    out['number'] = number
    while True:
        build = await _blocking(client.get_build, fullname=fullname, number=number)
        out.update(url=build.url, result=build.result, building=build.building)
        if not build.building:
            return done('finished')
        if time.monotonic() + _POLL_INTERVAL_SECONDS > deadline:
            return done('running', timed_out=True)
        await asyncio.sleep(_POLL_INTERVAL_SECONDS)
