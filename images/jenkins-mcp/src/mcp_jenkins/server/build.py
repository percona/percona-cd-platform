import base64
from typing import Literal

from fastmcp import Context
from requests.exceptions import HTTPError

from mcp_jenkins.core.lifespan import MasterArg, jenkins
from mcp_jenkins.server import mcp

# Caps that bound a single read so a tool call can never pull an unbounded payload or hand a
# pathological regex to the line filter.
_MAX_PATTERN_LEN = 2000
_MAX_HISTORY_COUNT = 100
_MAX_TAIL_LINES = 5000
_MAX_FAILURE_CASES = 1000

# JUnit case-status sets per failure-summary filter. UNSTABLE = the statuses Jenkins marks a build
# unstable for (FAILED + REGRESSION). None = no status filter (every case).
_FAILURE_STATUS_FILTERS: dict[str, set[str] | None] = {
    'UNSTABLE': {'FAILED', 'REGRESSION'},
    'FAILED': {'FAILED'},
    'REGRESSION': {'REGRESSION'},
    'NON_PASSED': {'FAILED', 'REGRESSION', 'SKIPPED'},
    'ALL': None,
}
# Narrow testReport projection: per-case identity + status + short errorDetails, NOT the heavy
# stdout/stderr/errorStackTrace, so a 15k-case parallel report does not come back whole.
_FAILURE_REPORT_TREE = 'failCount,passCount,skipCount,suites[name,cases[className,name,status,duration,errorDetails]]'


def _resolve_number(ctx: Context, fullname: str, number: int | None, master: MasterArg) -> int:
    """Resolve an explicit build number, or the job's last build number, or raise."""
    if number is None:
        number = jenkins(ctx, master).get_item(fullname=fullname, depth=1).lastBuild.number
    if number is None:
        msg = f'No build found for job: {fullname}'
        raise ValueError(msg)
    return number


def _is_wrapper_case(entry: dict) -> bool:
    """True if a single JUnit case rolls up an entire sub-runner so its FAILED status hides N real
    failures. The motivating case: MTR's `--unit-tests-report` folds a whole ctest run into one case
    (suite/className `...UNIT_TESTS.report`, name `unit_tests`), so failCount counts it as one. The
    real per-test failures are not in the JUnit report at all; they only appear in the console log.
    """
    cls = (entry.get('className') or '').lower()
    suite = (entry.get('suite') or '').lower()
    name = (entry.get('name') or '').lower()
    return (
        cls.endswith('.report')
        or suite.endswith('.report')
        or 'unit_tests' in cls
        or 'unit_tests' in suite
        or name == 'unit_tests'
    )


def _artifact_hint(relative_path: str) -> str | None:
    """Short content hint derived from an artifact's path, so the caller knows which artifact holds
    what without trial-and-error. Generic by design (build / test-runner / archive), with one
    specific pointer for an MTR-style per-test tarball (the documented in-archive-search case).
    Returns None when the path is not confidently recognizable, rather than mislabeling it.
    """
    base = (relative_path or '').rsplit('/', 1)[-1].lower()
    if ('mtr_logs' in base or 'test-mtr' in base) and base.endswith(('.tar.gz', '.tgz')):
        return 'per-test logs archive (use list_archive_artifact / grep_build_artifact archive_member=...)'
    if base.endswith(('.tar.gz', '.tgz', '.tar', '.zip')):
        return 'archive (use list_archive_artifact to see members)'
    if base.startswith(('junit', 'test-')) and base.endswith('.xml'):
        return 'JUnit report (use get_build_failure_summary for failures)'
    if base in ('build.log', 'build.log.gz'):
        return 'full build console log'
    if base == 'cmake.log' or base.startswith('make_') or (base.startswith('build') and base.endswith('.log')):
        return 'build log'
    if 'mtr' in base and base.endswith('.log'):
        return 'test-runner log (ctest/MTR console; per-test logs are in the *-mtr_logs-*.tar.gz)'
    if base.endswith(('.log.gz', '.gz')):
        return 'gzipped log'
    if base.endswith('.log'):
        return 'log'
    return None


@mcp.tool(tags=['read'])
async def get_running_builds(ctx: Context, master: MasterArg = None) -> list[dict]:
    """Get all running builds from Jenkins

    Returns:
        A list of all running builds
    """
    return [
        item.model_dump(include={'number', 'url', 'building', 'timestamp'})
        for item in jenkins(ctx, master).get_running_builds()
    ]


@mcp.tool(tags=['read'])
async def get_build(ctx: Context, fullname: str, number: int | None = None, master: MasterArg = None) -> dict:
    """Get specific build info from Jenkins

    Args:
        fullname: The fullname of the job
        number: The number of the build, if None, get the last build

    Returns:
        The build info
    """
    if number is None:
        number = jenkins(ctx, master).get_item(fullname=fullname, depth=1).lastBuild.number

    return jenkins(ctx, master).get_build(fullname=fullname, number=number).model_dump(exclude_none=True)


@mcp.tool(tags=['read'])
async def get_build_scripts(
    ctx: Context, fullname: str, number: int | None = None, master: MasterArg = None
) -> list[str]:
    """Get the scripts used in a specific build in Jenkins

    Args:
        fullname: The fullname of the job
        number: The number of the build, if None, get the last build

    Returns:
        A list of scripts used in the build
    """
    if number is None:
        number = jenkins(ctx, master).get_item(fullname=fullname, depth=1).lastBuild.number

    return jenkins(ctx, master).get_build_replay(fullname=fullname, number=number).scripts


@mcp.tool(tags=['read'])
async def get_build_console_output(
    ctx: Context,
    fullname: str,
    number: int | None = None,
    pattern: str | None = None,
    offset: int = 0,
    limit: int | None = None,
    master: MasterArg = None,
) -> str:
    """Get the console output of a specific build in Jenkins

    Args:
        fullname: The fullname of the job
        number: The number of the build, if None, get the last build
        pattern: Optional regex pattern to filter lines (only matching lines are returned)
        offset: Number of lines to skip from the beginning after filtering, default 0
        limit: Maximum number of lines to return after filtering and offset

    Returns:
        The console output of the build
    """
    if pattern and len(pattern) > _MAX_PATTERN_LEN:
        msg = f'pattern too long ({len(pattern)} > {_MAX_PATTERN_LEN} chars)'
        raise ValueError(msg)
    number = _resolve_number(ctx, fullname, number, master)

    return jenkins(ctx, master).get_build_console_output(
        fullname=fullname, number=number, pattern=pattern, offset=offset, limit=limit
    )


@mcp.tool(tags=['read'])
async def get_build_test_report(
    ctx: Context, fullname: str, number: int | None = None, master: MasterArg = None
) -> dict:
    """Get the test report of a specific build in Jenkins

    Args:
        fullname: The fullname of the job
        number: The number of the build, if None, get the last build

    Returns:
        The test report of the build
    """
    if number is None:
        number = jenkins(ctx, master).get_item(fullname=fullname, depth=1).lastBuild.number

    return jenkins(ctx, master).get_build_test_report(fullname=fullname, number=number)


@mcp.tool(tags=['read'])
async def get_build_failure_summary(
    ctx: Context,
    fullname: str,
    number: int | None = None,
    status_filter: Literal['UNSTABLE', 'FAILED', 'REGRESSION', 'NON_PASSED', 'ALL'] = 'UNSTABLE',
    max_cases: int = 200,
    include_error_details: bool = True,  # noqa: FBT001, FBT002
    max_error_chars: int = 1200,
    master: MasterArg = None,
) -> dict:
    """Summarize a build's failures in ONE call: result + stages + the failing tests, deduped.

    The right first tool for an UNSTABLE/FAILED build. It fuses build metadata, the pipeline stage
    breakdown, and ONLY the non-passing JUnit cases, so a 15k-pass report never comes back whole.
    For the full raw report use get_build_test_report.

    Args:
        fullname: The fullname of the job
        number: The number of the build, if None, get the last build
        status_filter: Which case statuses to include. UNSTABLE = FAILED + REGRESSION (default);
            FAILED / REGRESSION isolate one; NON_PASSED adds SKIPPED; ALL returns every case.
        max_cases: Cap on returned cases (capped at 1000); truncated is flagged.
        include_error_details: Include each case's short errorDetails message.
        max_error_chars: Truncate each errorDetails to this many characters.

    Returns:
        A dict with build, stages, test_counts, status_filter, cases (deduped), groups (by suite),
        included_count, truncated, notes, and partial. partial is True when a rolled-up "report"
        wrapper case (e.g. MTR --unit-tests-report -> ctest) hides the real per-test failures; then
        wrapper_cases lists them and a note + see_also point at the console where the real tests are.
    """
    max_cases = max(1, min(max_cases, _MAX_FAILURE_CASES))
    wanted = _FAILURE_STATUS_FILTERS[status_filter]
    client = jenkins(ctx, master)
    number = _resolve_number(ctx, fullname, number, master)

    build = client.get_build(fullname=fullname, number=number)
    stages = client.get_build_stages(fullname=fullname, number=number)

    notes: list[str] = []
    counts = {'total': 0, 'passCount': 0, 'failCount': 0, 'skipCount': 0}
    status_counts: dict[str, int] = {}
    selected: list[dict] = []
    seen: set[tuple[str, str]] = set()

    try:
        report = client.get_build_test_report(fullname=fullname, number=number, tree=_FAILURE_REPORT_TREE)
    except HTTPError as e:
        http_status = e.response.status_code if e.response is not None else None
        report = None
        notes.append(f'no test report (HTTP {http_status}); counts and cases are empty')

    if report:
        counts['failCount'] = report.get('failCount') or 0
        counts['skipCount'] = report.get('skipCount') or 0
        counts['passCount'] = report.get('passCount') or 0
        counts['total'] = counts['failCount'] + counts['skipCount'] + counts['passCount']
        for suite in report.get('suites') or []:
            suite_name = suite.get('name') or ''
            for case in suite.get('cases') or []:
                case_status = (case.get('status') or '').upper()
                status_counts[case_status] = status_counts.get(case_status, 0) + 1
                if wanted is not None and case_status not in wanted:
                    continue
                key = (case.get('className') or '', case.get('name') or '')
                if key in seen:
                    continue
                seen.add(key)
                entry = {
                    'name': key[1],
                    'className': key[0],
                    'status': case_status,
                    'suite': suite_name,
                    'duration': case.get('duration'),
                }
                if include_error_details and case.get('errorDetails'):
                    entry['errorDetails'] = case['errorDetails'][:max_error_chars]
                selected.append(entry)

    truncated = len(selected) > max_cases
    cases = selected[:max_cases]

    groups: dict[str, dict] = {}
    for case_entry in cases:
        group = groups.setdefault(case_entry['suite'], {'suite': case_entry['suite'], 'count': 0, 'cases': []})
        group['count'] += 1
        group['cases'].append(case_entry['name'])

    # A wrapper case counts as one failure but hides a whole sub-runner's real failures (see
    # _is_wrapper_case), so failCount understates reality. Flag it and point at the console, the only
    # place the real failing tests appear -- the JUnit report does not carry them.
    wrapper_cases = [
        {'name': c['name'], 'className': c['className'], 'suite': c['suite']} for c in cases if _is_wrapper_case(c)
    ]
    if wrapper_cases:
        labels = ', '.join(w['className'] or w['name'] for w in wrapper_cases)
        notes.append(
            f'{len(wrapper_cases)} rolled-up report case(s) ({labels}) each count as ONE failure but hide the '
            'real per-test results (e.g. MTR --unit-tests-report folds a whole ctest run into one case), so '
            'failCount understates reality. Get the real failing tests from the console via '
            'get_build_console_tail, or grep_build_artifact (pattern "The following tests FAILED" or "*** Failed").'
        )

    result = {
        'master': master,
        'fullname': fullname,
        'number': number,
        'build': build.model_dump(include={'url', 'result', 'building', 'timestamp', 'duration'}, exclude_none=True),
        'stages': [s.model_dump(exclude_none=True) for s in stages.stages],
        'test_counts': {**counts, 'status_counts': status_counts},
        'status_filter': status_filter,
        'included_count': len(cases),
        'truncated': truncated,
        'partial': bool(wrapper_cases),
        'cases': cases,
        'groups': sorted(groups.values(), key=lambda g: -g['count']),
        'notes': notes,
    }
    if wrapper_cases:
        result['wrapper_cases'] = wrapper_cases
        result['see_also'] = ['get_build_console_tail', 'grep_build_artifact']
    return result


@mcp.tool(tags=['read'])
async def get_build_console_tail(
    ctx: Context, fullname: str, number: int | None = None, lines: int = 200, master: MasterArg = None
) -> str:
    """Get the LAST N lines of a build's console (the cheap path to the failure tail + result).

    Streams the console server-side and returns only the trailing lines, so it is cheap even on a
    multi-MB log. The failure summary and `Finished: <result>` marker live at the end. Use
    get_build_console_output with a pattern to grep, or export_build_log for the whole log.

    Args:
        fullname: The fullname of the job
        number: The number of the build, if None, get the last build
        lines: Number of trailing lines to return (capped at 5000)

    Returns:
        The last `lines` lines of the console output.
    """
    lines = max(1, min(lines, _MAX_TAIL_LINES))
    number = _resolve_number(ctx, fullname, number, master)
    return jenkins(ctx, master).get_build_console_tail(fullname=fullname, number=number, lines=lines)


@mcp.tool(tags=['read'])
async def get_build_parameters(
    ctx: Context, fullname: str, number: int | None = None, master: MasterArg = None
) -> dict:
    """Get the parameters of a specific build in Jenkins

    Args:
        fullname: The fullname of the job
        number: The number of the build, if None, get the last build

    Returns:
        A dictionary of build parameter names and their values
    """
    if number is None:
        number = jenkins(ctx, master).get_item(fullname=fullname, depth=1).lastBuild.number

    return jenkins(ctx, master).get_build_parameters(fullname=fullname, number=number)


@mcp.tool(tags=['operate'])
async def stop_build(ctx: Context, fullname: str, number: int, master: MasterArg = None) -> None:
    """Stop a specific build in Jenkins

    Args:
        fullname: The fullname of the job
        number: The number of the build to stop
    """
    return jenkins(ctx, master).stop_build(fullname=fullname, number=number)


@mcp.tool(tags=['read'])
async def get_all_build_artifacts(
    ctx: Context, fullname: str, number: int | None = None, master: MasterArg = None
) -> list[dict]:
    """List the artifacts of a specific build in Jenkins

    Args:
        fullname: The fullname of the job
        number: The number of the build, if None, get the last build

    Returns:
        A list of artifact metadata dicts with fileName, relativePath, displayPath, and (when the
        path is recognizable) a short `hint` of what the artifact holds. For a `.tar.gz`, use
        list_archive_artifact to see members and grep_build_artifact(archive_member=...) to read one.
    """
    if number is None:
        number = jenkins(ctx, master).get_item(fullname=fullname, depth=1).lastBuild.number

    artifacts = []
    for artifact in jenkins(ctx, master).get_build_artifacts(fullname=fullname, number=number):
        entry = artifact.model_dump(exclude_none=True)
        hint = _artifact_hint(entry.get('relativePath', ''))
        if hint:
            entry['hint'] = hint
        artifacts.append(entry)
    return artifacts


@mcp.tool(tags=['read'])
async def get_build_artifact(
    ctx: Context, fullname: str, relative_path: str, number: int | None = None, master: MasterArg = None
) -> dict:
    """Download an artifact from a specific build in Jenkins

    Binary files are returned as base64-encoded content; text files are returned as plain text.

    Args:
        fullname: The fullname of the job
        relative_path: The relative path of the artifact (e.g. playwright-report/index.html)
        number: The number of the build, if None, get the last build

    Returns:
        A dict with 'content' (str) and 'encoding' ('utf-8' or 'base64')
    """
    if number is None:
        number = jenkins(ctx, master).get_item(fullname=fullname, depth=1).lastBuild.number

    content = jenkins(ctx, master).get_build_artifact(fullname=fullname, number=number, relative_path=relative_path)

    try:
        return {'content': content.decode('utf-8'), 'encoding': 'utf-8'}
    except UnicodeDecodeError:
        return {'content': base64.b64encode(content).decode('ascii'), 'encoding': 'base64'}


@mcp.tool(tags=['read'])
async def get_build_artifact_url(
    ctx: Context, fullname: str, relative_path: str, number: int | None = None, master: MasterArg = None
) -> str:
    """Get the direct URL of an artifact from a specific build in Jenkins

    Args:
        fullname: The fullname of the job
        relative_path: The relative path of the artifact (e.g. playwright-report/index.html)
        number: The number of the build, if None, get the last build

    Returns:
        The direct Jenkins URL of the artifact
    """
    if number is None:
        number = jenkins(ctx, master).get_item(fullname=fullname, depth=1).lastBuild.number

    return jenkins(ctx, master).get_build_artifact_url(fullname=fullname, number=number, relative_path=relative_path)


@mcp.tool(tags=['read'])
async def get_build_history(ctx: Context, fullname: str, count: int = 10, master: MasterArg = None) -> list[dict]:
    """Get the most recent builds of a Jenkins job.

    Args:
        fullname: The fullname of the job
        count: Maximum number of recent builds to return (most recent first)

    Returns:
        A list of builds with number, result, timestamp, duration, building, url
    """
    count = max(1, min(count, _MAX_HISTORY_COUNT))
    builds = jenkins(ctx, master).get_build_history(fullname=fullname, count=count)
    return [b.model_dump(exclude_none=True) for b in builds]


@mcp.tool(tags=['read'])
async def get_build_stages(ctx: Context, fullname: str, number: int | None = None, master: MasterArg = None) -> dict:
    """Get the pipeline stage breakdown of a build (which stage ran/failed and per-stage durations).

    Args:
        fullname: The fullname of the job
        number: The number of the build, if None, get the last build

    Returns:
        The run status plus a stages list; empty stages for a non-pipeline job
    """
    if number is None:
        number = jenkins(ctx, master).get_item(fullname=fullname, depth=1).lastBuild.number
    if number is None:
        raise ValueError(f'No build found for job: {fullname}')

    return jenkins(ctx, master).get_build_stages(fullname=fullname, number=number).model_dump(exclude_none=True)


@mcp.tool(tags=['read'])
async def get_build_changeset(
    ctx: Context, fullname: str, number: int | None = None, master: MasterArg = None
) -> list[dict]:
    """Get the SCM changes (commits) included in a build.

    Args:
        fullname: The fullname of the job
        number: The number of the build, if None, get the last build

    Returns:
        A list of changes: commitId, author, msg, timestamp, affectedPaths
    """
    if number is None:
        number = jenkins(ctx, master).get_item(fullname=fullname, depth=1).lastBuild.number
    if number is None:
        raise ValueError(f'No build found for job: {fullname}')

    changes = jenkins(ctx, master).get_build_changeset(fullname=fullname, number=number)
    return [c.model_dump(exclude_none=True) for c in changes]


@mcp.tool(tags=['operate'])
async def replay_build(ctx: Context, fullname: str, number: int | None = None, master: MasterArg = None) -> int:
    """Re-run a job using the same parameters as a previous build (retrigger a probe).

    Triggers a fresh build with the parameters of build `number` (or the last build). Does not
    modify the job config. Requires the jenkins-mcp-writers group.

    Args:
        fullname: The fullname of the job
        number: The build to copy parameters from; if None, the last build

    Returns:
        The queue item number of the newly triggered build.
    """
    if number is None:
        number = jenkins(ctx, master).get_item(fullname=fullname, depth=1).lastBuild.number
    if number is None:
        raise ValueError(f'No build found for job: {fullname}')

    params = jenkins(ctx, master).get_build_parameters(fullname=fullname, number=number)
    if params:
        return jenkins(ctx, master).build_item(fullname=fullname, build_type='buildWithParameters', data=params)
    return jenkins(ctx, master).build_item(fullname=fullname, build_type='build')
