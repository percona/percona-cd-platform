/**
 * psmdb-staging fence: strip every build trigger from every job, so the staging
 * copy never starts cron-scheduled or SCM-polled builds of production jobs.
 * Freestyle jobs expose removeTrigger(descriptor). Pipeline jobs keep their
 * triggers in PipelineTriggersJobProperty, which is removed whole.
 * Multibranch and organization folders keep branch-indexing and scan
 * triggers on the folder itself. getTriggers() returns a copy, so the backing
 * list is cleared directly. A re-run over stripped items is a no-op.
 */
import jenkins.model.Jenkins
import jenkins.model.ParameterizedJobMixIn
import org.jenkinsci.plugins.workflow.job.properties.PipelineTriggersJobProperty
import com.cloudbees.hudson.plugins.folder.computed.ComputedFolder

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
def triggersField = ComputedFolder.getDeclaredField('triggers')
triggersField.setAccessible(true)
def folderStripped = 0
Jenkins.get().getAllItems(ComputedFolder).each { folder ->
    def list = triggersField.get(folder)
    if (list != null && !list.isEmpty()) {
        folderStripped += list.size()
        list.clear()
        folder.save()
    }
}
println("[disable-triggers] removed ${stripped} job triggers and ${folderStripped} folder triggers")
