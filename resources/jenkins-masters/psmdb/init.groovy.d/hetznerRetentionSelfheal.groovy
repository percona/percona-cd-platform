/**
 * Cure Hetzner agents whose retention strategy degraded to Always, at boot
 * and then every 10 minutes.
 *
 * The Hetzner plugin's shutdown policy wraps its retention strategy in a
 * transient field, so a policy deserialized from the controller's persisted
 * config carries a null strategy. Agents bake that null in at creation and
 * core Slave maps a null field to RetentionStrategy.Always: the worker is
 * never reaped, lives for months, and fills its disk (observed 2026-08-07:
 * 16/16 agents immortal on cloud.cd, 13/13 on ps80.cd, 2/2 on pxc.cd).
 * Plugin v103.percona.30 fixes the creation paths; this guard cures agents
 * that predate the fix or slip through a future regression, and is a no-op
 * when there is nothing to cure.
 *
 * Agents on an idle-period policy legitimately never run with Always, so
 * Always is the degraded state for them. The cure fails closed: only
 * agents positively identified as idle-period are touched. Hour-wrap
 * agents keep their end-of-billing-hour semantics (their policy is immune
 * to the deserialization loss anyway), and an agent whose policy cannot
 * be read is skipped and logged, never silently rewritten.
 *
 * Registering a PeriodicWork after boot is safe on this core: verified
 * live 2026-08-07 on pg.cd, a work added via PeriodicWork.all().add()
 * from the Script Console fired within its first period (core's
 * PeriodicWorkExtensionListListener schedules late additions).
 *
 * Plugin classes are matched by simple class name, not imports, so this
 * script degrades to a no-op instead of failing evaluation on a master
 * where the Hetzner plugin is absent or disabled.
 */
import hudson.model.PeriodicWork
import hudson.slaves.CloudRetentionStrategy
import hudson.slaves.RetentionStrategy
import jenkins.model.Jenkins

import java.util.concurrent.TimeUnit

def cure = {
    def cured = [], skipped = []
    Jenkins.get().nodes.each { node ->
        // Per-node guard: one throwing node must not abort the pass for
        // the rest of the fleet or block timer registration at boot.
        try {
            if (!node.class.name.startsWith('cloud.dnation.jenkins.plugins.hetzner.')) {
                return
            }
            if (!(node.retentionStrategy instanceof RetentionStrategy.Always)) {
                return
            }
            def policy = null
            try {
                policy = node.template?.shutdownPolicy
            } catch (ignored) {
            }
            // Fail closed: cure only agents positively identified as
            // idle-period. Hour-wrap agents keep their end-of-hour
            // semantics, and an unreadable policy is skipped (and logged)
            // rather than silently rewritten to idle retention.
            if (policy?.class?.simpleName != 'IdlePeriodPolicy') {
                skipped << "${node.nodeName}(${policy?.class?.simpleName ?: 'unreadable-policy'})"
                return
            }
            node.setRetentionStrategy(new CloudRetentionStrategy((int) policy.idleMinutes))
            cured << node.nodeName
        } catch (Throwable perNode) {
            println "hetznerRetentionSelfheal: skipping ${node.nodeName}: ${perNode}"
        }
    }
    if (cured) {
        println "hetznerRetentionSelfheal: cured ${cured.size()} agent(s): ${cured.join(', ')}"
    }
    if (skipped) {
        println "hetznerRetentionSelfheal: left ${skipped.size()} Always agent(s) uncured: ${skipped.join(', ')}"
    }
}

try {
    cure()
} catch (Throwable bootPass) {
    // The boot pass must never prevent the periodic registration below.
    println "hetznerRetentionSelfheal: boot cure pass failed: ${bootPass}"
}

// Also cure agents created after boot (rehydrator, provisioning from a
// deserialized template). On live re-evaluation (iac deploy) the previous
// timer is REPLACED, not kept: an old timer would run the old script's
// closure forever, silently ignoring any hotfix in this file. The marker
// toString identifies our timer across evaluations.
def MARKER = 'percona-hetznerRetentionSelfheal'
def registry = PeriodicWork.all()
def stale = registry.findAll { it.toString() == MARKER }
stale.each { registry.remove(it) }
registry.add(new PeriodicWork() {
    long getRecurrencePeriod() {
        return TimeUnit.MINUTES.toMillis(10)
    }

    protected void doRun() {
        try {
            cure()
        } catch (Throwable t) {
            // Never let the cure kill the shared Timer thread.
            println "hetznerRetentionSelfheal: cure pass failed: ${t}"
        }
    }

    String toString() {
        return MARKER
    }
})
println(stale
        ? "hetznerRetentionSelfheal: replaced ${stale.size()} stale timer(s), cure re-registered (10m)"
        : 'hetznerRetentionSelfheal: periodic cure registered (10m)')
