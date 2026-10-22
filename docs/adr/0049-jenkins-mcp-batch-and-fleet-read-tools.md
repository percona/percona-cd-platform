# 0049 - Jenkins MCP batch and fleet-wide tools from measured usage

**Status:** Accepted
**Amends:** [ADR 0040](0040-jenkins-mcp-in-artifact-search-extract.md) (`grep_build_artifact` accepts a glob, the per-file caps stay)
**Related:** [ADR 0038](0038-jenkins-mcp-image-autodeploy.md) (the deploy path for the 0.10.0 image)

## Context

A 30-day read of the gateway's audit log (about 7,000 calls, 14 users) showed where callers spend calls the tooling should answer in one:

- One job's builds read one call at a time: about a third of all calls went to walks of 4 or more builds of one job (history, then stages and parameters per build).
- A build number is invisible after `build_item`: `QueueItem` dropped `executable`, so callers polled the queue and the history by hand.
- No cross-master lookup: callers ran `query_items` once per master, and a third of all errors were "No Jenkins master selected".
- Many artifacts of one build grepped with the same pattern, one call per file (MTR worker logs).
- Bulk config reads (300 configs in an hour) and bulk stops (172 `stop_build` calls in five bursts).

`scripts/jenkins-mcp-audit-patterns.py` reproduces these counts from Loki.

## Decision

Add batch tools that do the loop server-side, each bounded, each reporting partial failures inline instead of failing the call:

- `get_build_walk` (read): history, parameters, JUnit counts and queue ids in one REST request, stages per build under a time budget, at most 30 builds.
- `wait_for_build` (read): queue id to build number, then wait until finished, at most 60 s per call, never raises on timeout. `QueueItem` keeps `executable` and `cancelled`, and builds carry `queueId` as the fallback once Jenkins forgets a queue item.
- `find_items` (read): one fullname regex across every master the caller may reach, concurrently, 30 s budget, catch-all patterns rejected.
- `grep_build_artifact` glob: matched against the build's artifact list, never sent to Jenkins, every selected path validated again, at most 30 files, 200 matches, 256 MiB in total.
- `search_item_configs` (read, writers-gated like `get_item_config`): matching lines only, secret-bearing elements and encrypted values blanked, at most 100 jobs.
- `stop_builds` (operate): up to 50 explicit `{fullname, number}` targets, duplicates refused, a status per build.

Two rules hold for every tool that fans out:

- **Blocking work leaves the event loop.** A tool that makes more than one Jenkins request is a sync `def` (FastMCP runs it on a worker thread) or awaits each request through `anyio.to_thread.run_sync`. A blocking body on the loop freezes `/healthz` and the kubelet kills the pod, the failure behind the `search_build_logs` fix. `grep_build_artifact` moved to sync for this reason.
- **Scope is decided on the request context.** The caller's master scope comes from the token in a context variable. A plain thread pool does not carry it, so `allowed_masters()` there returns "unscoped" and a scoped service account would reach the whole fleet. `find_items` reads the scope and builds every client on the request context, and worker threads only run the request.

## Consequences

- 46 tools (38 read, 5 operate, 3 manage). Clients discover them through `get_readme` and the README.
- The audit line keeps `error_module`, the pydantic `loc` and `type` of a failed argument, and the names (never values) of dropped arguments, so argument mistakes are diagnosable from Loki.
- `search_item_configs` redacts single-line secret elements only. A plaintext secret split across lines in a config would pass through to a writer, who can already read the full config with `get_item_config`.
- Batch calls run longer than single calls (up to the budgets above). The fleet request timeout per Jenkins request is unchanged.
