# 0018 — Autonomous fleet management CLI surface (Jenkins masters)

**Status:** Accepted (2026-05-17)
**Related:** [ADR 0013](0013-push-from-masters-with-nginx-bearer.md) (push-model metrics that drive the CLI's readiness gate)

## Context

The Percona Jenkins fleet (10 active masters) is upgraded via plugin
rollouts that need to deploy when each master is "safe to restart".
Historically, that gate was a homegrown bash cron at
`~/.local/bin/hetzner-upgrade-idle-masters.sh` (scheduled every 30 min)
that used the Jenkins REST `executors -r` to count busy executors and
deployed via `scripts/deploy.sh` when count was 0.

Two incidents on 2026-05-15 / 2026-05-16 broke that gate:

1. **Zombie executors.** ps80.cd's `reconnect-workers #1498709` finished
   35 ms ago but its executor still held a stale `currentExecutable`
   reference (21+ hours). REST reported `busy=1`. The cron refused to
   deploy ps80 for hours despite it being effectively idle. Root cause:
   the REST API doesn't expose `Run.isBuilding()`; it only checks
   "is there a currentExecutable slot". The build-finished state never
   reaches the REST view.

2. **Long-hung builds claiming alive.** rel.cd had a Postgres nightly
   pipeline claiming `Run.isBuilding()=true` for 3.9 days; pmm.cd had
   36 PMM upgrade-test-runner builds hung for up to 6.4 days. The
   plain `isBuilding()` answer was honest about the API contract but
   useless for "safe to restart" gating because the underlying work
   was wedged.

Force-restart drained both masters in one stroke but killed 47 builds
indiscriminately. The cron stalled for 6+ hours because every tick saw
real busy executors (per `isBuilding()`) and refused to deploy.

The fleet needed:
- A detection gate that distinguishes real work from zombies AND hung
  pipelines.
- An operator surface for triage before destructive actions.
- A campaign manager that survives across cron invocations and can be
  resumed.

## Decision

The `jenkins` CLI (Rust binary at `~/.local/bin/jenkins`, source in
`nogueiraanderson/dotfiles/rust/jenkins`) gains five subcommands that
collectively let cron drive autonomous rollouts and let operators
intervene manually with full audit. All shipped 2026-05-16/17 across
~10 commits.

### `jenkins admin <inst> executors --real [--max-age <duration>]`

Four-status classification via Groovy + `Run.isBuilding()`:

| Status | Meaning |
|---|---|
| real | `isBuilding()=true` AND elapsed ≤ max-age (default 24h) |
| stuck | `isBuilding()=true` AND elapsed > max-age |
| zombie | `isBuilding()=false` (executor never released) |
| unknown | non-Run, non-PlaceholderExecutable Queue.Executable |

The cron readiness gate uses `real == 0` (stuck doesn't block; stuck
builds aren't progressing, so restarting them costs nothing).

### `jenkins admin [<inst>|--all] readiness`

Wraps `--real --max-age` + plugin version match + `/api/json` health
ping into a single `OK | BUSY | NEEDS_INTERVENTION` verdict. The cron
checks `OK` as the deploy gate. `--all` fans out across the fleet
with a kubectl-style table sorted with `NEEDS_INTERVENTION` first.

### `jenkins admin <inst> incidents [--older-than 24h]`

Lists builds older than threshold sorted by age desc with URL, age,
last-log-activity, template, node, stage. Read-only triage view used
by operators before any destructive action.

### `jenkins admin <inst> kill-stuck --older-than <dur> --reason <text> [--confirm]`

Gated bulk kill via `Run.interrupt()`. Refuses without `--reason`,
without `--confirm`, with `--older-than < 1h`, with `> 50 kills`
without `--force`, and when Jenkins is quieting-down or restarting.
Per-kill audit JSONL appended to `~/.local/state/jenkins-kill-stuck.log`
BEFORE the kill (success field patched after), so partial crashes
still leave a row. Used when triage confirms wedged work.

### `jenkins campaign <create|run|status|abort|list>`

Resumable rolling plugin upgrades. State lives under
`~/.local/state/jenkins-campaign/<id>/` with TOML config (immutable
after create) + JSON state (atomic save with `.bak` rotation) + fs2
exclusive lock against concurrent ticks. The `run` subcommand probes
each instance's plugin version + readiness, marks Done/Skipped/Errored
per instance, and persists state across cron invocations. Auto-deploy
is deferred behind a future `--auto-deploy` flag; current behaviour
is probe-and-mark only.

## Principles

1. **Default to dry-run.** Every destructive subcommand defaults to a
   preview mode. Operators must pass `--confirm` (kill-stuck) or
   `--slack-live` (readiness) or `--auto-deploy` (campaign) to actually
   mutate.
2. **Required reasons.** Any operation that interrupts work requires a
   `--reason` flag, recorded verbatim in the audit log + Jenkins
   build's interrupt cause.
3. **Refuse foot-guns.** Explicit gates: `--older-than < 1h` refused;
   `> 50 kills` requires `--force`; quietingDown / restarting state
   refuses immediately.
4. **Audit before act.** Audit log lines append BEFORE the destructive
   action, patched with success status after. A crash mid-flight leaves
   evidence.
5. **Single-shot CLI = boolean for automation.** The `readiness`
   subcommand exists so cron can check one exit code, not parse summary
   tables. Same `--llm` mode for shell consumption.
6. **Plugin-side detection is additive, not authoritative.** Plugin
   v103.percona.17 emits `hetzner_stuck_builds_total` (Counter, dedup-
   cached, increments once per crossing) + `hetzner_oldest_build_age_seconds`
   + `hetzner_jenkins_real_busy_executors`. Dashboards consume these
   for at-a-glance fleet state; the CLI remains the operator surface.

## Consequences

- Cron rollouts can drain wedged masters without manual intervention
  once `campaign run --auto-deploy` lands (currently pinned at
  probe-and-mark; auto-deploy planned for a follow-up after several
  dry-run cycles).
- Operators have a complete triage flow: `readiness` → `incidents` →
  `kill-stuck --dry-run` → `kill-stuck --confirm`, all auditable.
- Plugin metrics + CLI converge on the same correctness model
  (`Run.isBuilding()` is necessary but not sufficient; elapsed + log
  silence are co-signals).

## What this ADR does NOT decide

- Plugin v103.percona.18's heartbeat-based worker self-kill (Phase 3 of
  the plugin upgrade roadmap (private planning notes, not in this repo)).
  That's a separate decision because it actively tears down Hetzner VMs;
  ADR-worthy when it ships.
- Controller-side hung-pipeline detection (Workset C in the same roadmap).
  Not built yet; would warrant its own ADR if pursued.

## Verification

Live-verified on the 10-master fleet 2026-05-16:

```bash
$ jenkins admin --all readiness
instance|state|real|stuck|zombies|reason
pmm|NEEDS_INTERVENTION|8|31|0|stuck=31
rel|NEEDS_INTERVENTION|3|2|0|stuck=2
ps57-k8s|NEEDS_INTERVENTION|0|0|0|api_ping_failed
cloud|BUSY|18|0|0|real=18
psmdb|BUSY|2|0|0|real=2
pg|OK|0|0|0|idle
...
```

The CLI correctly identified pmm + rel as needing operator
intervention (the same masters that stalled the cron on 2026-05-16),
flagged cloud + psmdb as legitimately busy, and cleared the rest.
