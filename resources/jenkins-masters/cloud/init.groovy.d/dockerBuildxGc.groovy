// dockerBuildxGc.groovy  (PKG-1427)
//
// Thin controller-side scheduler for the buildx builder GC. ALL GC logic
// lives in the sibling dockerBuildxGc.sh (same init.groovy.d directory,
// delivered by the same S3 sync); this file only:
//   - schedules an hourly tick on the Jenkins Timer pool,
//   - selects ONLINE + IDLE workers with a docker label atom,
//   - launches the shell payload on each through its remoting channel
//     (Launcher/ProcStarter, no custom classes cross the channel),
//   - applies layered bounds (per-proc timeout, per-node timeout,
//     whole-sweep deadline, overlap guard) and logs classified outcomes.
//
// The payload file is re-read on EVERY tick, so shell-side changes take
// effect via the S3/SSM sync alone, with no re-evaluation of this file.
// Missing payload fails closed (tick skipped, WARNING logged).
//
// Safety model: the idle gate (checked at selection AND re-checked at
// launch) is the primary protection; workers are single-executor, so idle
// means no build is using docker. The payload's 24h age filter and
// protected-builder list are the backstop for the idle->busy race; see
// dockerBuildxGc.sh.
//
// Idempotent re-deploy: re-evaluating this file (boot / Script Console)
// bumps a generation token; the superseded task self-terminates on its
// next tick, so exactly one GC converges within one period.
//
// Pause without a restart (until the next controller restart re-arms it):
//   System.setProperty('dockerBuildxGc.gen', 'DISABLED')
// Durable rollback: replace this file with a stub that only sets the
// property above, apply + let the S3/SSM sync replace the EBS copy, then
// evaluate the stub once. Deleting the S3 objects alone is NOT enough: the
// sync is additive and the old EBS copies re-arm at the next boot.

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
final File PAYLOAD_FILE = new File(Jenkins.instance.rootDir, 'init.groovy.d/dockerBuildxGc.sh')

// Generation token: supersede any schedule left by an earlier eval of this file.
final String GEN = UUID.randomUUID().toString()
System.setProperty('dockerBuildxGc.gen', GEN)

// Run the payload on one agent; called from a pool thread, bounded by the
// caller. Re-validates the idle/online gate at launch time.
def runOnNode = { Computer computer, String script ->
    Node node = computer.node
    if (node == null || computer.channel == null) { return [state: 'skip', reason: 'channel-gone'] }
    if (computer.offline || !computer.idle) { return [state: 'skip', reason: 'busy-at-launch'] }
    def outputBuffer = new ByteArrayOutputStream()
    def listener = new StreamTaskListener(outputBuffer, StandardCharsets.UTF_8)
    def proc = node.createLauncher(listener).launch()
                   .cmds('bash', '-c', script)
                   .quiet(true)
                   .stdout(outputBuffer)
                   .start()
    int rc = proc.joinWithTimeout(PROC_TIMEOUT_SEC, TimeUnit.SECONDS, listener)
    String output = outputBuffer.toString('UTF-8')
    def ok = (output =~ /DOCKERBUILDXGC containers=(\d+) volumes=(\d+) parse_errors=(\d+) cache=(\S+)/)
    if (ok.find()) {
        return [state: 'ok', containers: ok.group(1) as int, volumes: ok.group(2) as int,
                parseErrors: ok.group(3) as int, cache: ok.group(4)]
    }
    def skip = (output =~ /DOCKERBUILDXGC SKIP reason=(\S+)/)
    if (skip.find()) { return [state: 'skip', reason: skip.group(1)] }
    return [state: 'err', rc: rc]
}

Runnable gcTick = {
    try {
        if (System.getProperty('dockerBuildxGc.gen') != GEN) {
            throw new CancellationException('superseded by newer dockerBuildxGc eval')
        }

        long nowMs = System.currentTimeMillis()
        if (nowMs < ((System.getProperty('dockerBuildxGc.sweepUntil', '0')) as long)) {
            LOG.warning('dockerBuildxGc: previous sweep still active; skipping this tick')
            return
        }
        System.setProperty('dockerBuildxGc.sweepUntil', (nowMs + SWEEP_DEADLINE_SEC * 1000L).toString())

        try {
            if (!PAYLOAD_FILE.isFile()) {
                LOG.warning("dockerBuildxGc: payload ${PAYLOAD_FILE} missing; skipping tick (fail closed)")
                return
            }
            String script = PAYLOAD_FILE.getText('UTF-8')

            int skippedBusy = 0
            List<Computer> targets = []
            Jenkins.instance.computers.each { Computer computer ->
                if (computer instanceof Jenkins.MasterComputer) { return }
                Node node = computer.node
                if (node == null) { return }
                if (computer.offline || computer.temporarilyOffline) { return }
                boolean docker = node.assignedLabels.any { it.name == 'docker' || it.name.startsWith('docker-') }
                if (!docker) { return }
                if (!computer.idle) { skippedBusy++; return }
                targets << computer
            }

            int cleaned = 0, containers = 0, volumes = 0, parseErrors = 0
            int skips = 0, timeouts = 0, errors = 0, deadlineSkips = 0
            long deadlineMs = nowMs + (SWEEP_DEADLINE_SEC - 120L) * 1000L

            ExecutorService pool = Executors.newFixedThreadPool(POOL_SIZE, { Runnable r ->
                Thread t = new Thread(r, 'dockerBuildxGc-node')
                t.daemon = true
                return t
            } as ThreadFactory)
            try {
                def futures = targets.collectEntries { Computer computer ->
                    [(computer): pool.submit({ runOnNode(computer, script) } as Callable)]
                }
                futures.each { Computer computer, future ->
                    long remainMs = deadlineMs - System.currentTimeMillis()
                    if (remainMs <= 0L) {
                        future.cancel(true)
                        deadlineSkips++
                        return
                    }
                    try {
                        def result = future.get(Math.min(NODE_TIMEOUT_SEC * 1000L, remainMs), TimeUnit.MILLISECONDS)
                        if (result?.state == 'ok') {
                            cleaned++
                            containers += result.containers
                            volumes += result.volumes
                            parseErrors += result.parseErrors
                            if (result.containers > 0 || result.volumes > 0) {
                                LOG.info("dockerBuildxGc: ${computer.name} removed ${result.containers} " +
                                         "builder(s), ${result.volumes} volume(s), engine cache freed ${result.cache}")
                            }
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

            LOG.info("dockerBuildxGc: tick targets=${targets.size()} skippedBusy=${skippedBusy} " +
                     "cleaned=${cleaned} containers=${containers} volumes=${volumes} " +
                     "parseErrors=${parseErrors} skips=${skips} timeouts=${timeouts} " +
                     "errors=${errors} deadlineSkips=${deadlineSkips}")
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
LOG.info("dockerBuildxGc: scheduled (gen=${GEN}, every ${PERIOD_SEC}s, payload=${PAYLOAD_FILE.name})")
