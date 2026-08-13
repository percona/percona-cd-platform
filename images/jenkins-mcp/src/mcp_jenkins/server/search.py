"""Cross-job build-log search: grep build consoles across jobs on one master.

Composes existing client primitives (query_items to select jobs, get_build_history to resolve real
build numbers, get_build_console_output for the streaming regex line filter), so it adds no new
client code. Read-only and cost-bounded: it requires a job pattern and caps jobs, builds, matches,
and wall-clock time, so it can never trigger an unbounded log crawl. Any bound that clips results is
surfaced in the returned summary, never applied silently.

This tool is deliberately a plain `def`, not `async def`. The Jenkins client is synchronous, and a
wide scan is hundreds of console downloads; as an `async def` with a sync body it ran on the event
loop and froze it for the whole scan, so /healthz stopped answering and the kubelet liveness probe
SIGKILLed the container mid-call, dropping every MCP session on the pod. FastMCP runs sync tools in
a worker thread (`call_sync_fn_in_threadpool`), so keeping this function sync is what keeps the loop
responsive. Do not add `async` back without moving the blocking work off the loop yourself.
"""

import re
import time
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from typing import TYPE_CHECKING, TypeVar

from fastmcp import Context
from requests.exceptions import RequestException

from mcp_jenkins.core.lifespan import MasterArg, jenkins
from mcp_jenkins.server import mcp

if TYPE_CHECKING:
    from mcp_jenkins.jenkins.model.build import Build
    from mcp_jenkins.jenkins.model.item import ItemType
    from mcp_jenkins.jenkins.rest_client import Jenkins

_T = TypeVar('_T')

_MAX_JOBS_LIMIT = 200
_BUILDS_PER_JOB_LIMIT = 5
_MATCHES_PER_BUILD_LIMIT = 100
_MAX_TOTAL_MATCHES_LIMIT = 500
_TIME_BUDGET_LIMIT = 300

# A console read cannot be cancelled from outside: requests' timeout is per-read inactivity, not a
# total transfer deadline, so a huge log that keeps trickling blocks its thread past any budget.
# Fetches therefore run in this pool and are waited on with a deadline; an overrunning one is
# abandoned to drain here while the scan returns. Spare workers exist so a drain does not stall the
# next scan. Running builds are excluded before this point, since their console never ends at all.
_FETCH_WORKERS = 8
_fetch_pool = ThreadPoolExecutor(max_workers=_FETCH_WORKERS, thread_name_prefix='log-scan')

# job_pattern values that would scan everything; rejected so a search is always narrowed.
_CATCH_ALL = {'', '.*', '.+', '.*?', '*'}


def _clamp(value: int, hard_max: int, name: str, notes: list[str]) -> int:
    clamped = max(1, min(value, hard_max))
    if clamped != value:
        notes.append(f'{name} adjusted from {value} to {clamped}')
    return clamped


def _fetch_lines(client: 'Jenkins', fullname: str, number: int, pattern: str, limit: int) -> list[str]:
    """Return one build's matching console lines. Runs in the fetch pool."""
    output = client.get_build_console_output(fullname=fullname, number=number, pattern=pattern, limit=limit)
    return [line for line in output.split('\n') if line] if output else []


def _bounded(fn: Callable[..., _T], *args: object, deadline: float) -> _T:
    """Run a blocking client call in the fetch pool, waiting only until the deadline.

    Every Jenkins call in a scan goes through here, not just the console read: a build-history
    request blocks too, and a scan whose budget expired inside one would otherwise return late
    while reporting that it finished in time.
    """
    remaining = deadline - time.monotonic()
    if remaining <= 0:
        raise TimeoutError
    future = _fetch_pool.submit(fn, *args)
    try:
        return future.result(timeout=remaining)
    except TimeoutError:
        future.cancel()  # it keeps draining in the pool; nothing waits on it
        raise


def _completed_builds(client: 'Jenkins', fullname: str, count: int) -> tuple[list['Build'], int]:
    """Return (newest finished builds of a job, how many running builds were skipped).

    Build history gives real build numbers, so a job whose older builds were rotated away is not
    probed with numbers that 404, and it carries `building`, which is what lets a still-running
    build be skipped. Grepping a running build is not merely incomplete: Jenkins keeps streaming its
    console as the build produces output, so the read never returns and pins its thread for the life
    of the build.
    """
    history = client.get_build_history(fullname=fullname, count=count)
    finished = [build for build in history if not build.building]
    return finished[:count], len(history) - len(finished)


def _scan(
    *,
    client: 'Jenkins',
    jobs: list['ItemType'],
    pattern: str,
    builds_per_job: int,
    matches_per_build: int,
    max_total_matches: int,
    deadline: float,
) -> dict:
    """Grep the selected jobs' recent finished builds, newest first, until a bound is hit.

    Fetches are issued one at a time; the pool exists to bound each wait, not to parallelise. Every
    wait uses the remaining budget as its timeout, so the scan stops on time even when a console
    read has wedged, and reports timed_out rather than running long.
    """
    results: list[dict] = []
    attempted_jobs: set[str] = set()
    total_matches = 0
    builds_scanned = 0
    builds_skipped_running = 0
    console_attempts = 0
    fetch_errors = 0
    truncated_matches = False
    timed_out = False

    for job in jobs:
        if total_matches >= max_total_matches:
            truncated_matches = True
            break
        if time.monotonic() >= deadline:
            timed_out = True
            break

        try:
            builds, skipped_running = _bounded(
                _completed_builds, client, job.fullname, builds_per_job, deadline=deadline
            )
        except TimeoutError:
            timed_out = True
            break
        except RequestException:
            fetch_errors += 1
            continue
        builds_skipped_running += skipped_running

        for build in builds:
            if total_matches >= max_total_matches:
                truncated_matches = True
                break

            attempted_jobs.add(job.fullname)
            console_attempts += 1
            remaining_matches = min(matches_per_build, max_total_matches - total_matches)
            try:
                lines = _bounded(
                    _fetch_lines,
                    client,
                    job.fullname,
                    build.number,
                    pattern,
                    remaining_matches,
                    deadline=deadline,
                )
            except TimeoutError:
                # The read outlived the budget. It drains in the pool while the scan returns: a
                # master this slow will not serve the remaining builds any faster.
                timed_out = True
                break
            except RequestException:
                # Build rotated away mid-scan, or a master blip on one log: skip it, never abort.
                fetch_errors += 1
                continue

            builds_scanned += 1
            if not lines:
                continue
            results.append({'job_fullname': job.fullname, 'build_number': build.number, 'matching_lines': lines})
            total_matches += len(lines)

        if timed_out:
            break

    return {
        'results': results,
        'jobs_scanned': len(attempted_jobs),
        'builds_scanned': builds_scanned,
        'builds_skipped_running': builds_skipped_running,
        'total_matches': total_matches,
        'fetch_errors': fetch_errors,
        # Only console reads count: a job skipped for having nothing finished to scan, or a job
        # whose history request failed, is not the same as every log read failing.
        'all_fetches_failed': console_attempts > 0 and builds_scanned == 0,
        'truncated_matches': truncated_matches,
        'timed_out': timed_out,
    }


@mcp.tool(tags=['read'])
def search_build_logs(
    ctx: Context,
    pattern: str,
    job_pattern: str,
    ignore_case: bool = False,  # noqa: FBT001, FBT002
    max_jobs: int = 25,
    builds_per_job: int = 1,
    matches_per_build: int = 20,
    max_total_matches: int = 100,
    time_budget_seconds: int = 60,
    folder_depth: int | None = None,
    master: MasterArg = None,
) -> dict:
    """Search build console logs across jobs on a master (grep over recent builds).

    Selects jobs whose fullname matches job_pattern, then greps the most recent finished build(s) of
    each for pattern. Builds still running are skipped: their console has no end, so reading one
    would hang. Read-only and cost-bounded: job_pattern is required and a catch-all is rejected;
    jobs, builds-per-job, matches, and wall-clock time are capped. A scan that runs out of budget
    returns the matches it already has with summary.timed_out set. Any clipping is in the summary.

    Args:
        pattern: Regex matched against each console line (only matching lines are returned).
        job_pattern: Regex selecting jobs by fullname. Required; a catch-all (e.g. ".*") is rejected.
        ignore_case: Case-insensitive line matching (applied to pattern).
        max_jobs: Max jobs to scan (capped at 200).
        builds_per_job: Recent finished builds to scan per job, newest first (capped at 5).
        matches_per_build: Max matching lines kept per build (capped at 100).
        max_total_matches: Global cap on matching lines across all jobs (capped at 500).
        time_budget_seconds: Wall-clock budget for job discovery plus the scan (capped at 300). On
            expiry the scan stops and returns partial results with summary.timed_out true. A slow
            discovery therefore leaves less budget for the scan, by design.
        folder_depth: Folder recursion depth for job selection (None = all levels).
        master: Which configured master to target.

    Returns:
        A dict with results (per job/build matching lines) and a summary including counts plus
        truncated_jobs / truncated_matches / timed_out flags, builds_skipped_running, fetch_errors,
        all_fetches_failed (every fetch failed, so an empty result is an outage rather than a clean
        miss), and any clamp notes.
    """
    if not job_pattern or job_pattern.strip() in _CATCH_ALL:
        msg = 'job_pattern is required and must not be a catch-all (e.g. ".*"); narrow it.'
        raise ValueError(msg)

    notes: list[str] = []
    max_jobs = _clamp(max_jobs, _MAX_JOBS_LIMIT, 'max_jobs', notes)
    builds_per_job = _clamp(builds_per_job, _BUILDS_PER_JOB_LIMIT, 'builds_per_job', notes)
    matches_per_build = _clamp(matches_per_build, _MATCHES_PER_BUILD_LIMIT, 'matches_per_build', notes)
    max_total_matches = _clamp(max_total_matches, _MAX_TOTAL_MATCHES_LIMIT, 'max_total_matches', notes)
    time_budget_seconds = _clamp(time_budget_seconds, _TIME_BUDGET_LIMIT, 'time_budget_seconds', notes)

    effective_pattern = f'(?i){pattern}' if ignore_case else pattern
    try:
        re.compile(effective_pattern)
    except re.error as e:
        msg = f'invalid pattern regex: {e}'
        raise ValueError(msg) from e
    client = jenkins(ctx, master)

    started = time.monotonic()
    deadline = started + time_budget_seconds

    matched = client.query_items(fullname_pattern=job_pattern, folder_depth=folder_depth)
    buildable = [job for job in matched if getattr(job, 'lastBuild', None) is not None]
    truncated_jobs = len(buildable) > max_jobs
    jobs = buildable[:max_jobs]

    scan = _scan(
        client=client,
        jobs=jobs,
        pattern=effective_pattern,
        builds_per_job=builds_per_job,
        matches_per_build=matches_per_build,
        max_total_matches=max_total_matches,
        deadline=deadline,
    )

    return {
        'pattern': pattern,
        'job_pattern': job_pattern,
        'master': master,
        'results': scan['results'],
        'summary': {
            'jobs_matched_pattern': len(matched),
            'jobs_with_last_build': len(buildable),
            'jobs_scanned': scan['jobs_scanned'],
            'jobs_with_matches': len({r['job_fullname'] for r in scan['results']}),
            'builds_scanned': scan['builds_scanned'],
            'builds_skipped_running': scan['builds_skipped_running'],
            'total_matches': scan['total_matches'],
            'fetch_errors': scan['fetch_errors'],
            'all_fetches_failed': scan['all_fetches_failed'],
            'truncated_jobs': truncated_jobs,
            'truncated_matches': scan['truncated_matches'],
            # Overrunning the budget anywhere counts, including in job discovery before the scan.
            'timed_out': scan['timed_out'] or time.monotonic() >= deadline,
            'elapsed_seconds': round(time.monotonic() - started, 1),
            'time_budget_seconds': time_budget_seconds,
            'notes': notes,
        },
    }
