/**
 * Write one INFO line per minute to jenkins.log so log liveness can be
 * measured per master.
 *
 * An idle master is legitimately silent for hours (ps80, ps57 and pxb
 * wrote nothing in 70 to 127 of one week's 168 hours), so "no log line
 * for N minutes" alone cannot tell an idle controller from one whose
 * java.util.logging root level was lowered at runtime, whose file handler
 * detached, or whose Alloy tail or Loki push died. This heartbeat gives
 * every master a floor of one line per minute. The jenkins-uptime chart
 * counts the lines that reach Loki per master (jenkins:master_logs_fresh)
 * and alerts when a master still pushes metrics but no line lands for 15
 * minutes.
 *
 * The line goes through the root JUL logger like Jenkins' own INFO output,
 * so whatever silences INFO silences the heartbeat too. logp pins the
 * source to a stable name; the default caller lookup would print the
 * anonymous Groovy class of this timer.
 *
 * Registering a PeriodicWork after boot is safe on this core (verified
 * live 2026-08-07 on pg.cd, see hetznerRetentionSelfheal.groovy). On live
 * re-evaluation (iac deploy) the previous timer is replaced through the
 * marker toString, never duplicated.
 */
import hudson.model.PeriodicWork

import java.util.concurrent.TimeUnit
import java.util.logging.Level
import java.util.logging.Logger

def MARKER = 'percona-logHeartbeat'
def registry = PeriodicWork.all()
def stale = registry.findAll { it.toString() == MARKER }
stale.each { registry.remove(it) }
registry.add(new PeriodicWork() {
    private final Logger heartbeat = Logger.getLogger('percona.jenkins.logHeartbeat')

    long getRecurrencePeriod() {
        return TimeUnit.MINUTES.toMillis(1)
    }

    long getInitialDelay() {
        return 0L
    }

    protected void doRun() {
        heartbeat.logp(Level.INFO, 'percona.jenkins.logHeartbeat', 'beat', 'heartbeat')
    }

    String toString() {
        return MARKER
    }
})
println(stale
        ? "logHeartbeat: replaced ${stale.size()} stale timer(s), heartbeat re-registered (1m)"
        : 'logHeartbeat: heartbeat registered (1m)')
