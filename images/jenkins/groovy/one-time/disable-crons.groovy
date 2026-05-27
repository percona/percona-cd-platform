// Remove all TimerTrigger (cron) from jobs to prevent
// the cloned instance from running builds that conflict
// with the original instance.
import jenkins.model.Jenkins
import hudson.triggers.TimerTrigger

def disabled = []
Jenkins.instance.getAllItems(Job).each { job ->
  def toRemove = job.triggers.findAll { k, v -> v instanceof TimerTrigger }
  toRemove.each { key, trigger ->
    disabled << "${job.fullName}: ${trigger.spec}"
    job.removeTrigger(trigger.descriptor)
  }
}
if (disabled) {
  println "Disabled ${disabled.size()} cron triggers:"
  disabled.each { println "  - ${it}" }
} else {
  println "No cron triggers found"
}

// Self-delete
new File("/var/jenkins_home/init.groovy.d/disable-crons.groovy").delete()
