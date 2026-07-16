// dockerBuildxGc.groovy  (PKG-1427)
//
// Master-side hourly GC for leaked docker buildx builders on docker workers.
// Pipelines that run `docker buildx create --use` without a matching
// `docker buildx rm` leave a RUNNING moby/buildkit container
// (buildx_buildkit_<name>0) plus a NAMED <container>_state volume holding
// build cache on the worker. Both are exempt from `docker system prune`, so
// they accumulate until the disk hits 100% and every docker build on the
// node fails at image pull.
//
// Every tick, for each ONLINE + IDLE worker with a docker label atom:
//   1. remove ANONYMOUS buildx_buildkit_* containers older than 24h
//      (docker rm -f) plus their deterministic <container>_state volume,
//   2. remove orphaned buildx_buildkit_*_state volumes older than 24h whose
//      container is already gone,
//   3. bounded cache prunes: `docker builder prune --filter until=48h` for
//      the engine build cache, and `docker buildx --builder <b> prune` for
//      each PROTECTED named builder present on the node.
//
// PROTECTED builders are never removed: shared named builders (created via
// the use-or-create idiom in jenkins-pipelines) are long-lived and REUSED,
// so container age says nothing about activity. Only their cache is pruned,
// age-bounded. Pipelines introducing a new named builder must extend
// PROTECTED_BUILDERS here.
//
// Safety model: the idle gate (checked at selection AND re-checked at
// launch) is the primary protection; workers are single-executor, so idle
// means no build is using docker. The 24h age filter is the backstop for
// the remaining idle->busy race: a build that starts mid-GC either uses a
// PROTECTED named builder (never removed) or creates an anonymous builder
// that is seconds old, far under the cutoff. Deliberately NO
// `docker system prune` in this automated path: it can race a concurrent
// job between image build and push. The GC never touches the per-user
// buildx current-builder pointer.
//
// Runs ON THIS CONTROLLER on the Jenkins Timer pool. The shell payload is
// launched on each agent through its remoting channel (Launcher/ProcStarter,
// no custom classes cross the channel), with layered bounds: a proc-level
// joinWithTimeout, an outer future.get() per node, a whole-sweep deadline,
// and an overlap guard that skips a tick while the previous sweep is still
// marked active. A node that times out or errors is logged at WARNING and
// skipped; the sweep continues. Nodes without usable docker report a
// classified SKIP reason (no-docker-cli / permission-denied /
// daemon-unreachable) so silent no-ops are visible in the tick summary.
//
// Idempotent re-deploy: re-evaluating this file (boot / Script Console)
// bumps a generation token; the previously scheduled task sees the mismatch
// on its next tick and self-terminates, so exactly one GC converges within
// one period.
//
// Pause without a restart (until the next controller restart re-arms it):
//   System.setProperty('dockerBuildxGc.gen', 'DISABLED')
// Durable rollback: replace THIS file with a stub that only sets the
// property above, apply + let the S3/SSM sync replace the EBS copy, then
// evaluate the stub once. Deleting the S3 object alone is NOT enough: the
// sync is additive and the old EBS copy would re-arm at next boot.

import hudson.model.Computer
import hudson.model.Node
import hudson.util.StreamTaskListener
import jenkins.model.Jenkins
import jenkins.util.Timer
import java.nio.charset.StandardCharsets
import java.util.concurrent.Callable
import java.util.concurrent.CancellationException
import java.util.concurrent.ExecutorService
import java.util.concurrent.Executors
import java.util.concurrent.ThreadFactory
import java.util.concurrent.TimeUnit
import java.util.concurrent.TimeoutException
import java.util.logging.Logger

final Logger LOG = Logger.getLogger('dockerBuildxGc')
final long PERIOD_SEC = 3600L          // hourly
final long INITIAL_DELAY_SEC = 300L    // skip the boot storm
final int POOL_SIZE = 6                // concurrent agent channels per tick
final int PROC_TIMEOUT_SEC = 45        // inner bound: the remote shell itself
final int NODE_TIMEOUT_SEC = 60        // outer bound: launch on a wedged channel
final long SWEEP_DEADLINE_SEC = 1500L  // whole-sweep budget (25 min < period)

// Generation token: supersede any schedule left by an earlier eval of this file.
final String GEN = UUID.randomUUID().toString()
System.setProperty('dockerBuildxGc.gen', GEN)

// Static payload executed on the agent (triple-single-quote: no Groovy
// interpolation, $-signs below are bash). Quiet classified no-op on nodes
// without usable docker; only ever touches buildx_buildkit_* names, and
// never the PROTECTED named builders (kept in sync with PROTECTED_RE).
final String SCRIPT = '''#!/usr/bin/env bash
set -euo pipefail

command -v docker >/dev/null 2>&1 || { echo "DOCKERBUILDXGC SKIP reason=no-docker-cli"; exit 0; }

if ! docker_info_err="$(docker info 2>&1 >/dev/null)"; then
  if [[ "${docker_info_err}" == *"ermission denied"* ]]; then
    echo "DOCKERBUILDXGC SKIP reason=permission-denied"
  else
    echo "DOCKERBUILDXGC SKIP reason=daemon-unreachable"
  fi
  exit 0
fi

readonly AGE_SECS=$((24 * 3600))
readonly CACHE_UNTIL=48h
readonly PROTECTED_RE='^buildx_buildkit_(multiarch|pmmbuilder)[0-9]*$'
readonly PROTECTED_BUILDERS='multiarch pmmbuilder'
now="$(date +%s)"
removed_containers=0
removed_volumes=0
parse_errors=0

# Stale ANONYMOUS buildkit containers (running or stopped), each with its
# deterministic <name>_state volume. The name filter is substring-based, so
# anchor explicitly; protected named builders are skipped outright.
while IFS= read -r container; do
  [[ -n "${container}" ]] || continue
  [[ "${container}" == buildx_buildkit_* ]] || continue
  [[ "${container}" =~ ${PROTECTED_RE} ]] && continue
  created_at="$(docker inspect -f '{{.Created}}' "${container}" 2>/dev/null)" || { parse_errors=$((parse_errors + 1)); continue; }
  created_epoch="$(date -d "${created_at}" +%s 2>/dev/null)" || { parse_errors=$((parse_errors + 1)); continue; }
  (( now - created_epoch >= AGE_SECS )) || continue

  if docker rm -f "${container}" >/dev/null 2>&1; then
    removed_containers=$((removed_containers + 1))
    if docker volume rm -f "${container}_state" >/dev/null 2>&1; then
      removed_volumes=$((removed_volumes + 1))
    fi
  fi
done < <(docker ps -a --filter 'name=buildx_buildkit' --format '{{.Names}}')

# Orphaned _state volumes whose container is already gone (same anchoring
# and protection rules; the volume filter is also substring-based).
while IFS= read -r volume; do
  [[ -n "${volume}" ]] || continue
  [[ "${volume}" == buildx_buildkit_*_state ]] || continue
  container="${volume%_state}"
  [[ "${container}" =~ ${PROTECTED_RE} ]] && continue
  docker container inspect "${container}" >/dev/null 2>&1 && continue
  volume_created="$(docker volume inspect -f '{{.CreatedAt}}' "${volume}" 2>/dev/null)" || { parse_errors=$((parse_errors + 1)); continue; }
  volume_epoch="$(date -d "${volume_created}" +%s 2>/dev/null)" || { parse_errors=$((parse_errors + 1)); continue; }
  (( now - volume_epoch >= AGE_SECS )) || continue

  if docker volume rm -f "${volume}" >/dev/null 2>&1; then
    removed_volumes=$((removed_volumes + 1))
  fi
done < <(docker volume ls -q --filter 'name=buildx_buildkit')

# Bounded engine build-cache prune (classic docker build; independent of
# buildx instances and of the current-builder pointer).
prune_output="$(docker builder prune -f --filter "until=${CACHE_UNTIL}" 2>/dev/null)" || prune_output=""
cache_freed="$(awk -F': ' '/Total reclaimed space/{print $2}' <<<"${prune_output}")"
[[ -n "${cache_freed}" ]] || cache_freed="0B"

# Bound each protected named builder's cache without touching the builder.
for builder in ${PROTECTED_BUILDERS}; do
  docker buildx inspect "${builder}" >/dev/null 2>&1 || continue
  named_prune="$(docker buildx --builder "${builder}" prune -f --filter "until=${CACHE_UNTIL}" 2>/dev/null)" || continue
  named_freed="$(awk -F': ' '/Total reclaimed space/{print $2}' <<<"${named_prune}")"
  [[ -n "${named_freed}" ]] || named_freed="0B"
  echo "DOCKERBUILDXGC_NAMED builder=${builder} cache_freed=${named_freed}"
done

echo "DOCKERBUILDXGC containers=${removed_containers} volumes=${removed_volumes} parse_errors=${parse_errors} cache=${cache_freed}"
'''

// Nodes eligible this tick: real agents, online, not admin-quiesced, idle,
// with an exact docker label atom ('docker' or 'docker-*'). The payload's
// own docker guard is the backstop for anything mislabelled.
def isDockerLabelled = { Node node ->
    return node.assignedLabels.any { it.name == 'docker' || it.name.startsWith('docker-') }
}

def selectTargets = { Closure labelled ->
    def targets = []
    int skippedBusy = 0
    Jenkins.instance.computers.each { Computer computer ->
        if (computer instanceof Jenkins.MasterComputer) { return }
        Node node = computer.node
        if (node == null) { return }
        if (computer.offline || computer.temporarilyOffline) { return }
        if (!labelled(node)) { return }
        if (!computer.idle) { skippedBusy++; return }
        targets << computer
    }
    return [targets: targets, skippedBusy: skippedBusy]
}

// Parse the payload's summary lines. rc==0 with neither summary nor SKIP
// (e.g. bash missing) still surfaces as an error, not a silent pass.
def parseSummary = { int rc, String output ->
    def ok = (output =~ /DOCKERBUILDXGC containers=(\d+) volumes=(\d+) parse_errors=(\d+) cache=(\S+)/)
    if (ok.find()) {
        def named = output.readLines().findAll { it.contains('DOCKERBUILDXGC_NAMED') }
        return [state: 'ok',
                containers: ok.group(1) as int,
                volumes: ok.group(2) as int,
                parseErrors: ok.group(3) as int,
                cache: ok.group(4),
                named: named]
    }
    def skip = (output =~ /DOCKERBUILDXGC SKIP reason=(\S+)/)
    if (skip.find()) { return [state: 'skip', reason: skip.group(1)] }
    return [state: 'err', rc: rc]
}

// Run the payload on one agent. Called from a pool thread; the caller bounds
// the whole call. Re-validates the idle/online gate at launch time to shrink
// the selection->execution race window.
def runOnNode = { Computer computer ->
    Node node = computer.node
    if (node == null || computer.channel == null) { return [state: 'skip', reason: 'channel-gone'] }
    if (computer.offline || !computer.idle) { return [state: 'skip', reason: 'busy-at-launch'] }
    def outputBuffer = new ByteArrayOutputStream()
    def listener = new StreamTaskListener(outputBuffer, StandardCharsets.UTF_8)
    def launcher = node.createLauncher(listener)
    def proc = launcher.launch()
                       .cmds('bash', '-c', SCRIPT)
                       .quiet(true)
                       .stdout(outputBuffer)
                       .start()
    int rc = proc.joinWithTimeout(PROC_TIMEOUT_SEC, TimeUnit.SECONDS, listener)
    return parseSummary(rc, outputBuffer.toString('UTF-8'))
}

Runnable gcTick = {
    try {
        if (System.getProperty('dockerBuildxGc.gen') != GEN) {
            throw new CancellationException('superseded by newer dockerBuildxGc eval')
        }

        long nowMs = System.currentTimeMillis()
        long sweepUntil = (System.getProperty('dockerBuildxGc.sweepUntil', '0')) as long
        if (nowMs < sweepUntil) {
            LOG.warning('dockerBuildxGc: previous sweep still active; skipping this tick')
            return
        }
        System.setProperty('dockerBuildxGc.sweepUntil', (nowMs + SWEEP_DEADLINE_SEC * 1000L).toString())

        try {
            def selection = selectTargets(isDockerLabelled)
            List<Computer> targets = selection.targets
            int cleanedNodes = 0, totalContainers = 0, totalVolumes = 0, totalParseErrors = 0
            int skips = 0, timeouts = 0, errors = 0, deadlineSkips = 0
            long deadlineMs = nowMs + (SWEEP_DEADLINE_SEC - 120L) * 1000L

            ExecutorService pool = Executors.newFixedThreadPool(POOL_SIZE, { Runnable r ->
                Thread t = new Thread(r, 'dockerBuildxGc-node')
                t.daemon = true
                return t
            } as ThreadFactory)
            try {
                def futures = targets.collectEntries { Computer computer ->
                    [(computer): pool.submit({ runOnNode(computer) } as Callable)]
                }
                futures.each { Computer computer, future ->
                    long remainMs = deadlineMs - System.currentTimeMillis()
                    if (remainMs <= 0L) {
                        future.cancel(true)
                        deadlineSkips++
                        return
                    }
                    try {
                        long waitMs = Math.min(NODE_TIMEOUT_SEC * 1000L, remainMs)
                        def result = future.get(waitMs, TimeUnit.MILLISECONDS)
                        if (result?.state == 'ok') {
                            cleanedNodes++
                            totalContainers += result.containers
                            totalVolumes += result.volumes
                            totalParseErrors += result.parseErrors
                            if (result.containers > 0 || result.volumes > 0) {
                                LOG.info("dockerBuildxGc: ${computer.name} removed " +
                                         "${result.containers} builder(s), ${result.volumes} volume(s), " +
                                         "engine cache freed ${result.cache}")
                            }
                            result.named.each { String line -> LOG.fine("dockerBuildxGc: ${computer.name} ${line}") }
                        } else if (result?.state == 'skip') {
                            skips++
                            LOG.fine("dockerBuildxGc: ${computer.name} skipped (${result.reason})")
                        } else if (result?.state == 'err') {
                            errors++
                            LOG.warning("dockerBuildxGc: ${computer.name} payload rc=${result.rc}")
                        }
                    } catch (TimeoutException te) {
                        future.cancel(true)
                        timeouts++
                        LOG.warning("dockerBuildxGc: ${computer.name} timed out (wedged channel?); skipping")
                    } catch (Exception nodeErr) {
                        errors++
                        LOG.warning("dockerBuildxGc: ${computer.name} errored: ${nodeErr.message}")
                    }
                }
            } finally {
                pool.shutdownNow()
            }

            LOG.info("dockerBuildxGc: tick targets=${targets.size()} " +
                     "skippedBusy=${selection.skippedBusy} cleaned=${cleanedNodes} " +
                     "containers=${totalContainers} volumes=${totalVolumes} " +
                     "parseErrors=${totalParseErrors} skips=${skips} " +
                     "timeouts=${timeouts} errors=${errors} deadlineSkips=${deadlineSkips}")
        } finally {
            System.setProperty('dockerBuildxGc.sweepUntil', '0')
        }
    } catch (CancellationException supersede) {
        throw supersede   // let scheduleAtFixedRate suppress this stale generation
    } catch (Throwable t) {
        LOG.warning("dockerBuildxGc: unexpected ${t}")   // swallow so the timer keeps running
    }
}

Timer.get().scheduleAtFixedRate(gcTick, INITIAL_DELAY_SEC, PERIOD_SEC, TimeUnit.SECONDS)
LOG.info("dockerBuildxGc: scheduled (gen=${GEN}, every ${PERIOD_SEC}s, docker-labelled idle nodes)")
