"""Cross-job build-log search: grep build consoles across jobs on one master.

Composes the existing query_items (select jobs by name pattern) and get_build_console_output
(server-side regex line filter) primitives, so it adds no new client code. Read-only and
cost-bounded: it requires a job pattern and caps jobs, builds, and matches scanned, so it can
never trigger an unbounded log crawl. Any bound that clips results is surfaced in the returned
summary, never applied silently.
"""

from fastmcp import Context
from requests.exceptions import HTTPError

from mcp_jenkins.core.lifespan import MasterArg, jenkins
from mcp_jenkins.server import mcp

_MAX_JOBS_LIMIT = 200
_BUILDS_PER_JOB_LIMIT = 5
_MATCHES_PER_BUILD_LIMIT = 100
_MAX_TOTAL_MATCHES_LIMIT = 500

# job_pattern values that would scan everything; rejected so a search is always narrowed.
_CATCH_ALL = {'', '.*', '.+', '.*?', '*'}


def _clamp(value: int, hard_max: int, name: str, notes: list[str]) -> int:
    clamped = max(1, min(value, hard_max))
    if clamped != value:
        notes.append(f'{name} adjusted from {value} to {clamped}')
    return clamped


@mcp.tool(tags=['read'])
async def search_build_logs(
    ctx: Context,
    pattern: str,
    job_pattern: str,
    ignore_case: bool = False,  # noqa: FBT001, FBT002
    max_jobs: int = 25,
    builds_per_job: int = 1,
    matches_per_build: int = 20,
    max_total_matches: int = 100,
    folder_depth: int | None = None,
    master: MasterArg = None,
) -> dict:
    """Search build console logs across jobs on a master (grep over recent builds).

    Selects jobs whose fullname matches job_pattern, then greps the most recent build(s) of each
    for pattern. Read-only and cost-bounded: job_pattern is required and a catch-all is rejected;
    jobs, builds-per-job, and matches are capped. Any clipping is reported in the summary.

    Args:
        pattern: Regex matched against each console line (only matching lines are returned).
        job_pattern: Regex selecting jobs by fullname. Required; a catch-all (e.g. ".*") is rejected.
        ignore_case: Case-insensitive line matching (applied to pattern).
        max_jobs: Max jobs to scan (capped at 200).
        builds_per_job: Recent builds to scan per job, newest first (capped at 5).
        matches_per_build: Max matching lines kept per build (capped at 100).
        max_total_matches: Global cap on matching lines across all jobs (capped at 500).
        folder_depth: Folder recursion depth for job selection (None = all levels).

    Returns:
        A dict with results (per job/build matching lines) and a summary including counts plus
        truncated_jobs / truncated_matches flags and any clamp notes.
    """
    if not job_pattern or job_pattern.strip() in _CATCH_ALL:
        msg = 'job_pattern is required and must not be a catch-all (e.g. ".*"); narrow it.'
        raise ValueError(msg)

    notes: list[str] = []
    max_jobs = _clamp(max_jobs, _MAX_JOBS_LIMIT, 'max_jobs', notes)
    builds_per_job = _clamp(builds_per_job, _BUILDS_PER_JOB_LIMIT, 'builds_per_job', notes)
    matches_per_build = _clamp(matches_per_build, _MATCHES_PER_BUILD_LIMIT, 'matches_per_build', notes)
    max_total_matches = _clamp(max_total_matches, _MAX_TOTAL_MATCHES_LIMIT, 'max_total_matches', notes)

    effective_pattern = f'(?i){pattern}' if ignore_case else pattern
    client = jenkins(ctx, master)

    matched = client.query_items(fullname_pattern=job_pattern, folder_depth=folder_depth)
    buildable = [j for j in matched if getattr(j, 'lastBuild', None) is not None]
    truncated_jobs = len(buildable) > max_jobs
    jobs = buildable[:max_jobs]

    results: list[dict] = []
    total_matches = 0
    jobs_scanned = 0
    builds_scanned = 0
    truncated_matches = False

    for job in jobs:
        jobs_scanned += 1
        last = job.lastBuild.number
        for number in range(last, max(last - builds_per_job, 0), -1):
            if total_matches >= max_total_matches:
                truncated_matches = True
                break
            remaining = min(matches_per_build, max_total_matches - total_matches)
            try:
                output = client.get_build_console_output(
                    fullname=job.fullname, number=number, pattern=effective_pattern, limit=remaining
                )
            except HTTPError:
                continue  # build rotated or unavailable; skip, do not abort the scan
            builds_scanned += 1
            lines = [ln for ln in output.split('\n') if ln] if output else []
            if lines:
                results.append({'job_fullname': job.fullname, 'build_number': number, 'matching_lines': lines})
                total_matches += len(lines)
        if total_matches >= max_total_matches:
            truncated_matches = True
            break

    return {
        'pattern': pattern,
        'job_pattern': job_pattern,
        'master': master,
        'results': results,
        'summary': {
            'jobs_matched_pattern': len(buildable),
            'jobs_scanned': jobs_scanned,
            'jobs_with_matches': len(results),
            'builds_scanned': builds_scanned,
            'total_matches': total_matches,
            'truncated_jobs': truncated_jobs,
            'truncated_matches': truncated_matches,
            'notes': notes,
        },
    }
