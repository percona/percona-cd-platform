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
 * Plugin v103.percona.29 fixes the creation paths; this guard cures agents
 * that predate the fix or slip through a future regression, and is a no-op
 * when there is nothing to cure.
 *
 * A Hetzner agent legitimately never runs with Always (its policies yield
 * CloudRetentionStrategy or the hour-wrap strategy), so Always is always
 * the degraded state here.
 */
import hudson.model.PeriodicWork
import hudson.slaves.CloudRetentionStrategy
import hudson.slaves.RetentionStrategy
import jenkins.model.Jenkins

import java.util.concurrent.TimeUnit

def cure = {
    def cured = []
    Jenkins.get().nodes.each { node ->
        if (!node.class.name.startsWith('cloud.dnation.jenkins.plugins.hetzner.')) {
            return
        }
        if (!(node.retentionStrategy instanceof RetentionStrategy.Always)) {
            return
        }
        int idleMinutes = 10
        try {
            def policy = node.template?.shutdownPolicy
            if (policy?.class?.simpleName == 'IdlePeriodPolicy') {
                idleMinutes = policy.idleMinutes
            }
        } catch (ignored) {
        }
        node.setRetentionStrategy(new CloudRetentionStrategy(idleMinutes))
        cured << node.nodeName
    }
    if (cured) {
        println "hetznerRetentionSelfheal: cured ${cured.size()} agent(s): ${cured.join(', ')}"
    }
}

cure()

// Also cure agents created after boot (rehydrator, provisioning from a
// deserialized template). Guard against duplicate registration when the
// script is re-evaluated live via iac deploy: the marker property survives
// re-evaluation and resets with the JVM, exactly like the PeriodicWork list.
if (System.getProperty('percona.hetznerRetentionSelfheal.registered') != 'true') {
    PeriodicWork.all().add(new PeriodicWork() {
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
    })
    System.setProperty('percona.hetznerRetentionSelfheal.registered', 'true')
    println 'hetznerRetentionSelfheal: periodic cure registered (10m)'
}
