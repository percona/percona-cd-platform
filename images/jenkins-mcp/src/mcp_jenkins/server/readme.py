"""A self-describing usage guide, exposed as the get_readme tool.

Lets an MCP client learn this server's capabilities, the configured master fleet, how to
select a master per call, the access tiers, sample queries, and tips, without guessing or
reaching for an external Jenkins CLI. The master fleet is injected live from the loaded
config, and the tool catalog is filtered to the tools this instance actually serves (so the
guide can never advertise a tool you cannot call).
"""

from mcp_jenkins.core.fleet import get_fleet
from mcp_jenkins.core.scope import visible_masters
from mcp_jenkins.server import mcp

_OPERATE_TOOLS = ('build_item', 'replay_build', 'stop_build', 'stop_builds', 'cancel_queue_item')
_MANAGE_TOOLS = ('create_item', 'set_item_config', 'delete_item')

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
                'query_items(fullname_pattern?, label_pattern?, include_parameters?, ..., master?)',
            ),
            (
                'query_items',
                '      filter jobs by name/class/color; label_pattern = the agent label (where arch lives),',
            ),
            (
                'query_items',
                '      freestyle/matrix only (pipeline jobs keep their label in the Jenkinsfile, so they never match).',
            ),
            (
                'find_items',
                'find_items(fullname_pattern, class_pattern?, masters?, limit?)   WHICH MASTER hosts a job: searches',
            ),
            ('find_items', '      every master you can reach at once (no master argument needed)'),
            ('get_item', 'get_item(fullname, master?)             one job/folder'),
            ('get_item_config', 'get_item_config(fullname, master?)      job config XML (jenkins-mcp-writers only)'),
            (
                'search_item_configs',
                'search_item_configs(pattern, fullname_pattern, ...)   grep many job configs, matching lines only,',
            ),
            ('search_item_configs', '      secrets blanked (jenkins-mcp-writers only)'),
            ('get_item_parameters', 'get_item_parameters(fullname, master?)  the build parameters a job accepts'),
        ],
    ),
    (
        'Builds',
        [
            ('get_running_builds', 'get_running_builds(master?)             unique builds running now'),
            ('get_build', 'get_build(fullname, number?, master?)   build result/info (last build if number omitted)'),
            (
                'get_build_failure_summary',
                'get_build_failure_summary(fullname, number?, status_filter?, master?)',
            ),
            (
                'get_build_failure_summary',
                '      START HERE for UNSTABLE/FAILED: result + stages + ONLY the failing tests (deduped, grouped);',
            ),
            (
                'get_build_failure_summary',
                '      sets partial=true when a rolled-up wrapper case (e.g. MTR unit-tests) hides real failures',
            ),
            (
                'get_build_console_output',
                'get_build_console_output(fullname, number?, pattern?, offset?, limit?, master?)',
            ),
            (
                'get_build_console_output',
                '      console log; pattern is a regex line filter (slice big logs with pattern/limit)',
            ),
            (
                'get_build_console_tail',
                'get_build_console_tail(fullname, number?, lines?, master?)  last N console lines (the failure tail)',
            ),
            (
                'get_build_test_report',
                'get_build_test_report / get_build_parameters / get_build_scripts(fullname, number?, master?)',
            ),
            (
                'get_build_test_report',
                '      get_build_test_report is the RAW full report; prefer get_build_failure_summary for failures',
            ),
            (
                'get_all_build_artifacts',
                'get_all_build_artifacts / get_build_artifact / get_build_artifact_url(fullname, ..., master?)',
            ),
            (
                'get_all_build_artifacts',
                '      each artifact carries a content hint; a .tar.gz -> list_archive_artifact / grep_build_artifact',
            ),
            (
                'get_build_history',
                'get_build_history(fullname, count?, master?)  build numbers + timing only (summaries: get_build_walk)',
            ),
            (
                'get_build_walk',
                'get_build_walk(fullname, count?, include?, master?)   last N builds in ONE call: result + stages',
            ),
            (
                'get_build_walk',
                '      + parameters + causes + test counts per build (instead of history + a call per build)',
            ),
            (
                'get_build_stages',
                'get_build_stages(fullname, number?, master?)     pipeline stage breakdown (empty for freestyle)',
            ),
            ('get_build_changeset', 'get_build_changeset(fullname, number?, master?)  SCM commits included in a build'),
            (
                'export_build_log',
                'export_build_log(fullname, number?, master?)     stream the WHOLE log to S3, returns a signed URL',
            ),
            (
                'export_build_artifact',
                'export_build_artifact(fullname, relative_path, number?, master?)  artifact -> S3 signed URL',
            ),
        ],
    ),
    (
        'Search',
        [
            (
                'search_build_logs',
                'search_build_logs(pattern, job_pattern, master?, ...)  grep build consoles across jobs (cost-bounded)',
            ),
            (
                'grep_build_artifact',
                'grep_build_artifact(fullname, relative_path, pattern, archive_member?, number?, master?, ...)',
            ),
            (
                'grep_build_artifact',
                '      grep inside ONE artifact (or a member of a .tar.gz via archive_member); streamed + capped',
            ),
            (
                'grep_build_artifact',
                '      relative_path may be a glob ("results/mtr-test-w_*.log") to grep up to 30 artifacts at once',
            ),
            (
                'list_archive_artifact',
                'list_archive_artifact(fullname, relative_path, glob?, number?, master?)  list members of a .tar.gz',
            ),
            (
                'extract_archive_artifact',
                'extract_archive_artifact(fullname, relative_path, member, number?, master?)  one member -> inline/S3',
            ),
        ],
    ),
    (
        'Infra',
        [
            ('get_all_nodes', 'get_all_nodes / get_node / get_node_config(name?, master?)   build agents'),
            ('get_all_queue_items', 'get_all_queue_items / get_queue_item(id?, master?)           build queue'),
            (
                'wait_for_build',
                'wait_for_build(fullname, queue_id? | number?, timeout_seconds?, master?)   queue id -> build number,',
            ),
            ('wait_for_build', '      then wait (<= 60 s per call) until it finishes; never raises on timeout'),
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
                'stop_builds',
                'stop_builds(items=[{fullname, number}], master?)   abort up to 50 builds, a status per build',
            ),
            (
                'cancel_queue_item',
                'cancel_queue_item(id, master?)                     drop a queued (not-yet-running) build',
            ),
        ],
    ),
    (
        'Manage (job definitions -- jenkins-mcp-writers only)',
        [
            (
                'create_item',
                'create_item(fullname, config_xml, master?)      create a new job/folder from config.xml',
            ),
            (
                'set_item_config',
                'set_item_config(fullname, config_xml, master?)  overwrite an existing job/folder config.xml',
            ),
            (
                'delete_item',
                'delete_item(fullname, master?)                  delete a job/folder and its build history',
            ),
        ],
    ),
]

_SAMPLES_READ = [
    '  "which masters are up?"                       -> list_masters()',
    '  "what jobs are on a master?" -> get_all_items(master="<master>")',
    '  "which master runs <job>?" -> find_items(fullname_pattern="<job>")',
    '  "jobs whose name matches a pattern" -> query_items(fullname_pattern=".*nightly.*", master="<master>")',
    '  "jobs by agent label" -> query_items(label_pattern="<label>", master="<master>")',
    '  "list files in a .tar.gz artifact" -> list_archive_artifact(relative_path="<path>.tar.gz", glob="*.log")',
    '  "grep a .tar.gz member" -> grep_build_artifact(relative_path="a.tgz", archive_member="<m>", pattern="ERROR")',
    '  "grep every worker log" -> grep_build_artifact(relative_path="results/mtr-test-w_*.log", pattern="Failing")',
    '  "did the last build of <job> pass?" -> get_build(fullname="<job>")',
    '  "console for <job> build 123" -> get_build_console_output(fullname="<job>", number=123)',
    '  "just the errors in a log" -> get_build_console_output(fullname="<job>", pattern="(?i)error|fail")',
    '  "why is <job> failing / what failed?" -> get_build_failure_summary(fullname="<job>")',
    '  "the end of the <job> log" -> get_build_console_tail(fullname="<job>", lines=200)',
    '  "what parameters does <job> take?" -> get_item_parameters(fullname="<job>")',
    '  "parameters of every <x> job" -> query_items(fullname_pattern="<x>", include_parameters=True)',
    '  "parameters of every job on a master" -> query_items(include_parameters=True, compact_parameters=True),',
    '      then page=1 up to pages - 1 when the result reports more than one page',
    '  "what is running right now?" -> get_running_builds(master="<master>")',
    '  "summarise the last 10 builds of <job>" -> get_build_walk(fullname="<job>", count=10)',
    '  "just the build numbers of <job>" -> get_build_history(fullname="<job>", count=10)',
    '  "compare the last 20 runs of <job>" -> get_build_walk(fullname="<job>", count=20)',
    '  "who started the last builds of <job>?" -> get_build_walk(fullname="<job>", include=["causes"])',
    '  "which stage failed in <job>?" -> get_build_stages(fullname="<job>")',
    '  "what changed in <job> build 50?" -> get_build_changeset(fullname="<job>", number=50)',
    '  "find text across recent builds" -> search_build_logs(pattern="<text>", job_pattern="<prefix>.*")',
    '  "download the full log of <job>" -> export_build_log(fullname="<job>")',
    '  "signed link to an artifact" -> export_build_artifact(fullname="<job>", relative_path="dist/app.tar.gz")',
]

_SAMPLES_OPERATE = [
    '  "rebuild <job>"            -> build_item(fullname="<job>", build_type="build")',
    '  "which build did that start?" -> wait_for_build(fullname="<job>", queue_id=<id from build_item>)',
    '  "build with params"      -> build_item(fullname="<job>", build_type="buildWithParameters", data={"K":"V"})',
    '  "replay the last build"    -> replay_build(fullname="<job>")',
    '  "stop <job> build 123"     -> stop_build(fullname="<job>", number=123)',
    '  "stop these 5 builds"      -> stop_builds(items=[{"fullname": "<job>", "number": 123}, ...])',
    '  "cancel queue item 45"     -> cancel_queue_item(id=45)',
]

_SAMPLES_MANAGE = [
    '  "download a job config"  -> get_item_config(fullname="<job>")',
    '  "which jobs set X?"      -> search_item_configs(pattern="UPLOAD_HOST", fullname_pattern="^pmm3-")',
    '  "create a job from XML"  -> create_item(fullname="<folder/job>", config_xml="<config.xml>")',
    '  "update a job config"    -> set_item_config(fullname="<job>", config_xml="<config.xml>")',
    '  "delete a job"           -> delete_item(fullname="<job>")',
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
    build-lifecycle operate and job-definition manage for the jenkins-mcp-writers group, and no
    node-config or script-console mutation), the tool catalog filtered to what this instance serves,
    sample queries, and tips. Call this first whenever you are unsure which tool to use.
    """
    fleet = ', '.join(visible_masters(get_fleet().names())) or '(none configured)'
    served = await _served_tool_names()
    operate_served = served is None or any(t in served for t in _OPERATE_TOOLS)
    manage_served = served is None or any(t in served for t in _MANAGE_TOOLS)

    def _tier_status(is_served: bool) -> str:  # noqa: FBT001
        if served is None:
            return 'served only on operate-mode deployments (run list_masters/list-tools to confirm here).'
        if is_served:
            return 'ENABLED on this instance (you still need the jenkins-mcp-writers group to call it).'
        return 'not enabled on this instance (read-only deployment).'

    operate_status = _tier_status(operate_served)
    manage_status = _tier_status(manage_served)

    def is_served(name: str) -> bool:
        return served is None or name in served

    lines = [
        '# Jenkins MCP -- token-free fleet access (start here)',
        '',
        "Token-free gateway to Percona's Jenkins masters. You log in once via Authentik (JumpCloud) in the",
        'browser; the server holds the Jenkins credentials, so you never handle a token. Every call is',
        'attributed to your Authentik identity and audited, even though the gateway uses one shared',
        'Jenkins credential downstream.',
        '',
        f'Masters configured: {fleet}',
        'Call list_masters() for live reachability and Jenkins core versions.',
        '',
        '## Access tiers',
        '  - Read (everyone): inspect jobs, builds, logs, nodes, queue, views across the whole fleet.',
        '    Exception: get_item_config and search_item_configs (config.xml) need jenkins-mcp-writers, since',
        '    config.xml can carry plaintext secrets (e.g. the <authToken> trigger-builds-remotely token).',
        f'  - Operate (build lifecycle): build / replay / stop / cancel. {operate_status}',
        f'  - Manage (create / update / delete job definitions): {manage_status}',
        '    Backend create/configure/delete is NOT granted, so these return 403 today. Manage job',
        '    definitions through the Jenkins API/CLI directly for now (see PS-11341).',
        '  - To join jenkins-mcp-writers: ask in #opensource-jenkins. After you are added, disconnect and',
        '    re-authenticate your MCP client; reconnecting alone keeps the old group-less token.',
        '  - Node config and Groovy script-console mutation are NEVER exposed here, in any mode. There is',
        '    no tool for them. Use the Jenkins UI for that.',
        '',
        '## Pick a master (per call)',
        'Every per-master tool takes an optional `master` argument:',
        '  - omit it       -> the default master (the one pinned in your MCP config, else the server default)',
        '  - master="<name>"  -> target that master for this one call (must be a configured name above)',
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
    if manage_served:
        lines += ['manage (jenkins-mcp-writers, when enabled):', *_SAMPLES_MANAGE]

    lines += [
        '',
        '## Tips',
        '  - Job names are FULL folder paths, e.g. "folder/sub/job"; find exact names with query_items.',
        '  - get_all_items / query_items return a flat compact list capped by limit (total + truncated reported).',
        '  - On a big master, narrow with query_items(fullname_pattern=...) rather than raising limit.',
        '  - number defaults to the most recent build when omitted (except stop_build, which needs one).',
        '  - Reads are open to any authenticated Percona user (JumpCloud SSO login); the exceptions are',
        '    get_item_config and search_item_configs (config.xml), which need the jenkins-mcp-writers group.',
        '  - Operate and manage tools additionally need the jenkins-mcp-writers group (refused otherwise).',
        '  - A manage writer can define a job that runs code on a master, so the group is code-execution-capable.',
        '  - export_build_log / export_build_artifact stream big logs/artifacts to a short-lived signed S3 URL.',
        '  - For a failing build use get_build_failure_summary (just the failures), not the raw test report.',
        '  - In-artifact (streamed + capped): grep_build_artifact greps a log or a .tar.gz member (literal default,',
        '    regex opt-in); list_archive_artifact lists members; extract_archive_artifact pulls one (inline or S3).',
        '  - query_items(label_pattern=...) filters jobs by agent label (arch), but only freestyle/matrix jobs expose',
        '    one; pipeline jobs keep their label in the Jenkinsfile, so they will not match.',
    ]
    return '\n'.join(lines)
