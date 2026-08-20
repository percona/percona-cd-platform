/**
 * ps57-upgraded clone fence: strip every build trigger from every job so the
 * clone never starts cron-scheduled or SCM-polled builds of production job
 * copies. Covers all trigger types (TimerTrigger, SCMTrigger, plugin
 * triggers), unlike the ps3-k8s precedent which removed timers only.
 * Persistent and idempotent: a re-run over already-stripped jobs is a no-op,
 * which matters because the 30-minute S3 init sync keeps this file in place.
 */
import jenkins.model.Jenkins
import jenkins.model.ParameterizedJobMixIn

def stripped = 0
Jenkins.get().getAllItems(ParameterizedJobMixIn.ParameterizedJob).each { job ->
    def triggers = new ArrayList(job.getTriggers().values())
    triggers.each { trigger ->
        job.removeTrigger(trigger.getDescriptor())
        stripped++
        println("[disable-triggers] ${job.fullName}: removed ${trigger.getClass().simpleName}")
    }
    if (!triggers.isEmpty()) {
        job.save()
    }
}
println("[disable-triggers] removed ${stripped} triggers")
