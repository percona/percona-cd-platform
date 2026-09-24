/**
 * pmm-staging fence: strip every build trigger from every job, so the staging
 * copy never starts cron-scheduled or SCM-polled builds of production jobs.
 * Freestyle jobs expose removeTrigger(descriptor). Pipeline jobs keep their
 * triggers in PipelineTriggersJobProperty, which is removed whole. A re-run
 * over already stripped jobs is a no-op.
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
