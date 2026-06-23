import base64

from fastmcp import Context

from mcp_jenkins.core.lifespan import MasterArg, jenkins
from mcp_jenkins.server import mcp


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
    if number is None:
        number = jenkins(ctx, master).get_item(fullname=fullname, depth=1).lastBuild.number
    if number is None:
        raise ValueError(f'No build found for job: {fullname}')

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
        A list of artifact metadata dicts with fileName, relativePath, and displayPath
    """
    if number is None:
        number = jenkins(ctx, master).get_item(fullname=fullname, depth=1).lastBuild.number

    return [
        artifact.model_dump(exclude_none=True)
        for artifact in jenkins(ctx, master).get_build_artifacts(fullname=fullname, number=number)
    ]


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
