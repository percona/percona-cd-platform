/**
 * ps80-upgraded clone fence: strip every build trigger from every job so the
 * clone never starts cron-scheduled or SCM-polled builds of production job
 * copies. Covers all trigger types (TimerTrigger, SCMTrigger, ReverseBuildTrigger,
 * plugin triggers). Freestyle jobs expose removeTrigger(descriptor); pipeline
 * jobs (WorkflowJob) do not, their triggers live in PipelineTriggersJobProperty,
 * which is removed whole. Persistent and idempotent: a re-run over already
 * stripped jobs is a no-op, which matters because the 30-minute S3 init sync
 * keeps this file in place.
 */
import jenkins.model.Jenkins
import jenkins.model.ParameterizedJobMixIn
import org.jenkinsci.plugins.workflow.job.properties.PipelineTriggersJobProperty

def stripped = 0
Jenkins.get().getAllItems(ParameterizedJobMixIn.ParameterizedJob).each { job ->
    def triggers = new ArrayList(job.getTriggers().values())
    if (triggers.isEmpty()) {
        return
    }
    if (job.metaClass.respondsTo(job, 'removeTrigger', hudson.triggers.TriggerDescriptor)) {
        triggers.each { trigger ->
            job.removeTrigger(trigger.getDescriptor())
        }
    } else {
        job.removeProperty(PipelineTriggersJobProperty)
    }
    job.save()
    stripped += triggers.size()
    println("[disable-triggers] ${job.fullName}: removed ${triggers.collect { it.getClass().simpleName }}")
}
println("[disable-triggers] removed ${stripped} triggers")
