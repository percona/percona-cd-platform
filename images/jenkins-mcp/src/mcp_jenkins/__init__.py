import asyncio
import os
import sys
from pathlib import Path

import click
from loguru import logger

try:
    LOG_DIR = Path.home() / '.mcp_jenkins'
    logger.add(LOG_DIR / 'log.log', rotation='10 MB')
except Exception as e:  # noqa: BLE001
    logger.error(f'Failed to set up logger directory: {e}')

if sys.platform == 'win32':
    asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())


@click.command()
@click.option(
    '--jenkins-master',
    default='',
    help='Default master name for per-master tools when no x-jenkins-master header is sent',
)
@click.option(
    '--jenkins-fleet-file',
    default='',
    help='Path to the fleet config JSON (masters + read-only tokens); sets MCP_JENKINS_FLEET_FILE',
)
@click.option(
    '--read-only',
    default=False,
    is_flag=True,
    help='Whether to run in read-only mode, default is False',
)
@click.option(
    '--enable-operate',
    default=False,
    is_flag=True,
    help='Enable build-lifecycle (operate) tools after a fail-closed permission preflight; '
    'still gated to the jenkins-mcp-writers group per call. Mutually exclusive with --read-only.',
)
@click.option(
    '--jenkins-session-singleton/--no-jenkins-session-singleton',
    default=True,
    help='In the same session, reuse the per-master Jenkins client, reducing instantiations and crumb requests',
)
@click.option(
    '--transport',
    type=click.Choice(['stdio', 'sse', 'streamable-http']),
    default='stdio',
)
@click.option(
    '--host',
    default='0.0.0.0',
    help='Host to bind to for SSE or Streamable HTTP transport',
)  # noqa: S104
@click.option(
    '--port',
    default=9887,
    help='Port to listen on for SSE or Streamable HTTP transport',
)
def main(
    jenkins_master: str,
    jenkins_fleet_file: str,
    read_only: bool,  # noqa: FBT001
    enable_operate: bool,  # noqa: FBT001
    jenkins_session_singleton: bool,  # noqa: FBT001
    transport: str,
    host: str,
    port: int,
) -> None:
    if jenkins_master:
        os.environ['jenkins_master'] = jenkins_master
    if jenkins_fleet_file:
        os.environ['MCP_JENKINS_FLEET_FILE'] = jenkins_fleet_file

    os.environ['jenkins_session_singleton'] = str(jenkins_session_singleton).lower()

    from mcp_jenkins.server import mcp

    if read_only:
        mcp.enable(tags={'read'}, only=True)
    elif enable_operate:
        from mcp_jenkins.core.fleet import write_preflight

        violations = write_preflight()
        if violations:
            for v in violations:
                logger.error(f'write preflight violation: {v}')
            logger.error('Refusing operate (write) mode: the Jenkins service identity is over-privileged.')
            sys.exit(1)
        mcp.enable(tags={'read', 'operate'}, only=True)
        logger.info('Operate (write) mode enabled: read + operate tools, gated to the jenkins-mcp-writers group.')

    if transport == 'stdio':
        asyncio.run(mcp.run_async(transport=transport))
    elif transport in ('sse', 'streamable-http'):
        asyncio.run(mcp.run_async(transport=transport, host=host, port=port))


if __name__ == '__main__':
    main()
