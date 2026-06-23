"""A self-describing usage guide, exposed as the get_readme tool.

Lets an MCP client learn this server's capabilities, the configured master fleet, how to
select a master per call, the access tiers, sample queries, and tips, without guessing or
reaching for an external Jenkins CLI. The master fleet is injected live from the loaded
config, and the tool catalog is filtered to the tools this instance actually serves (so the
guide can never advertise a tool you cannot call).
"""

from mcp_jenkins.core.fleet import get_fleet
from mcp_jenkins.server import mcp

_OPERATE_TOOLS = ('build_item', 'replay_build', 'stop_build', 'cancel_queue_item')

# Curated catalog: (group heading, [(tool_name, description_line)...]). Lines are filtered to
# the served set at call time; a group with no served line is dropped. Multi-tool read lines are
# keyed by their first tool (read tools are never partially stripped, so the bundle is all-or-none).
_CATALOG = [
    (
        'Jobs',
        [
            (
                'get_all_items',
                'get_all_items(limit?, master?)          flat compact list of jobs/folders (fullname/class/color)',
            ),
            (
                'query_items',
                'query_items(fullname_pattern?, color_pattern?, class_pattern?, folder_depth?, limit?, master?)',
            ),
            (
                'query_items',
                '      filter jobs by name/class/color; flat compact list. Both return {items, total, truncated}.',
            ),
            ('get_item', 'get_item(fullname, master?)             one job/folder'),
            ('get_item_config', 'get_item_config(fullname, master?)      job config XML'),
            ('get_item_parameters', 'get_item_parameters(fullname, master?)  the build parameters a job accepts'),
        ],
    ),
    (
        'Builds',
        [
            ('get_running_builds', 'get_running_builds(master?)             what is building now'),
            ('get_build', 'get_build(fullname, number?, master?)   build result/info (last build if number omitted)'),
            (
                'get_build_console_output',
                'get_build_console_output(fullname, number?, pattern?, offset?, limit?, master?)',
            ),
            (
                'get_build_console_output',
                '      console log; pattern is a regex line filter (slice big logs with pattern/limit)',
            ),
            (
                'get_build_test_report',
                'get_build_test_report / get_build_parameters / get_build_scripts(fullname, number?, master?)',
            ),
            (
                'get_all_build_artifacts',
                'get_all_build_artifacts / get_build_artifact / get_build_artifact_url(fullname, ..., master?)',
            ),
            (
                'get_build_history',
                'get_build_history(fullname, count?, master?)     recent builds (number/result/timestamp/duration)',
            ),
            (
                'get_build_stages',
                'get_build_stages(fullname, number?, master?)     pipeline stage breakdown (empty for freestyle)',
            ),
            ('get_build_changeset', 'get_build_changeset(fullname, number?, master?)  SCM commits included in a build'),
        ],
    ),
    (
        'Search',
        [
            (
                'search_build_logs',
                'search_build_logs(pattern, job_pattern, master?, ...)  grep build consoles across jobs (cost-bounded)',
            ),
        ],
    ),
    (
        'Infra',
        [
            ('get_all_nodes', 'get_all_nodes / get_node / get_node_config(name?, master?)   build agents'),
            ('get_all_queue_items', 'get_all_queue_items / get_queue_item(id?, master?)           build queue'),
            ('get_all_views', 'get_all_views / get_view(view_path, depth?, master?)         views'),
        ],
    ),
    (
        'Operate (build lifecycle -- jenkins-mcp-writers only)',
        [
            (
                'build_item',
                'build_item(fullname, build_type, data?, master?)   trigger a build ("build" or "buildWithParameters")',
            ),
            (
                'replay_build',
                'replay_build(fullname, number?, master?)           re-run a build with the same revision/params',
            ),
            ('stop_build', 'stop_build(fullname, number, master?)              abort a running build'),
            (
                'cancel_queue_item',
                'cancel_queue_item(id, master?)                     drop a queued (not-yet-running) build',
            ),
        ],
    ),
]

_SAMPLES_READ = [
    '  "which masters are up?"                       -> list_masters()',
    '  "what jobs are on pxc?"                        -> get_all_items(master="pxc")',
    '  "pxc jobs (incl. folders) matching 8.0"        -> query_items(fullname_pattern=".*8.0.*", master="pxc")',
    '  "did the last build of <job> pass?"            -> get_build(fullname="<job>")',
    '  "console for <job> build 123"                  -> get_build_console_output(fullname="<job>", number=123)',
    '  "just the errors in a log"   -> get_build_console_output(fullname="<job>", pattern="(?i)error|fail")',
    '  "what parameters does <job> take?"             -> get_item_parameters(fullname="<job>")',
    '  "what is running on ps80 right now?"           -> get_running_builds(master="ps80")',
    '  "last 10 builds of <job>"  -> get_build_history(fullname="<job>", count=10)',
    '  "which stage failed in <job>?"  -> get_build_stages(fullname="<job>")',
    '  "what changed in <job> build 50?"  -> get_build_changeset(fullname="<job>", number=50)',
    '  "find OOMKilled in pxc jobs"  -> search_build_logs(pattern="OOMKilled", job_pattern="pxc.*", master="pxc")',
]

_SAMPLES_OPERATE = [
    '  "rebuild <job>"            -> build_item(fullname="<job>", build_type="build")',
    '  "build with params"      -> build_item(fullname="<job>", build_type="buildWithParameters", data={"K":"V"})',
    '  "replay the last build"    -> replay_build(fullname="<job>")',
    '  "stop <job> build 123"     -> stop_build(fullname="<job>", number=123)',
    '  "cancel queue item 45"     -> cancel_queue_item(id=45)',
]


async def _served_tool_names() -> set[str] | None:
    """The names this instance actually serves (after the CLI tag filter), or None if unknown.

    list_tools() is the same enumeration the MCP protocol exposes, so it reflects --read-only /
    --enable-operate exactly. Returns None (rather than raising) if it cannot be read, so the guide
    degrades to a mode-agnostic view instead of breaking.
    """
    try:
        tools = await mcp.list_tools()
    except Exception:  # noqa: BLE001
        return None
    return {t.name for t in tools}


@mcp.tool(tags=['read'])
async def get_readme() -> str:
    """Start here: how to use this token-free Jenkins fleet MCP.

    Returns a concise guide covering what this server is, the configured masters, how to select a
    master per call (the optional `master` argument), the access tiers (read for everyone,
    build-lifecycle operate for the jenkins-mcp-writers group, and no config/script mutation at
    all), the tool catalog filtered to what this instance serves, sample queries, and tips. Call
    this first whenever you are unsure which tool to use or how to query the Jenkins fleet.
    """
    fleet = ', '.join(get_fleet().names()) or '(none configured)'
    served = await _served_tool_names()
    operate_served = served is None or any(t in served for t in _OPERATE_TOOLS)

    if served is None:
        operate_status = 'served only on operate-mode deployments (run list_masters/list-tools to confirm here).'
    elif operate_served:
        operate_status = 'ENABLED on this instance (you still need the jenkins-mcp-writers group to call it).'
    else:
        operate_status = 'not enabled on this instance (read-only deployment).'

    def is_served(name: str) -> bool:
        return served is None or name in served

    lines = [
        '# Jenkins MCP -- token-free fleet access (start here)',
        '',
        "Token-free gateway to Percona's Jenkins masters. You log in once via Authentik (Duo) in the",
        'browser; the server holds the Jenkins credentials, so you never handle a token. Every call is',
        'attributed to your Authentik identity and audited, even though the gateway uses one shared',
        'Jenkins credential downstream.',
        '',
        f'Masters configured: {fleet}',
        'Call list_masters() for live reachability and Jenkins core versions.',
        '',
        '## Access tiers',
        '  - Read (everyone): inspect jobs, builds, logs, nodes, queue, views across the whole fleet.',
        f'  - Operate (build lifecycle): build / replay / stop / cancel. {operate_status}',
        '  - Config + script mutation (edit/delete a job, node config, Groovy) is NEVER exposed here, in',
        '    any mode. There is no tool for it. Use the Jenkins UI for that.',
        '',
        '## Pick a master (per call)',
        'Every per-master tool takes an optional `master` argument:',
        '  - omit it       -> the default master (the one pinned in your MCP config, else the server default)',
        '  - master="pxc"  -> target that master for this one call (must be a configured name above)',
        'These tools cover the whole fleet; there is no separate Jenkins CLI to reach for.',
        '',
        '## Tools',
    ]

    for heading, entries in _CATALOG:
        # A desc that starts with whitespace is a wrapped continuation of the line above: indent it
        # without a bullet. Primary entries get the "  - " bullet.
        group_lines = [
            f'      {desc.lstrip()}' if desc[:1] == ' ' else f'  - {desc}' for name, desc in entries if is_served(name)
        ]
        if group_lines:
            lines.append(f'{heading}:')
            lines.extend(group_lines)

    lines += ['', '## Sample queries', *_SAMPLES_READ]
    if operate_served:
        lines += ['operate (jenkins-mcp-writers, when enabled):', *_SAMPLES_OPERATE]

    lines += [
        '',
        '## Tips',
        '  - Job names are FULL paths incl. folders, e.g. "PXC/pxc-8.0/build". Discover exact names with query_items.',
        '  - get_all_items / query_items return a flat compact list capped by limit (total + truncated reported).',
        '  - On a big master, narrow with query_items(fullname_pattern=...) rather than raising limit.',
        '  - number defaults to the most recent build when omitted (except stop_build, which needs one).',
        '  - Reads are open to any authenticated Percona user (Duo SSO login).',
        '  - Operate tools additionally need the jenkins-mcp-writers group (refused otherwise).',
    ]
    return '\n'.join(lines)
