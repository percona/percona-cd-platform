/**
 * rel-staging fence. Runs FIRST (init.groovy.d is alphabetical) on every boot.
 * JENKINS_HOME is restored from a snapshot of production rel, so at JVM start
 * config.xml still carries production's clouds and nodes/ carries its live
 * agent records. This script:
 *
 *  - clears ALL clouds, so nothing provisions or reaps production workers,
 *  - removes every inherited agent, so no launcher reconnects to a production
 *    worker and no idle retention terminates one,
 *  - sets built-in executors to 0, so nothing builds on the controller,
 *  - puts Jenkins in quiet-down, so no build starts, not even the
 *    controller-side part of a pipeline,
 *  - hard-kills every pipeline resumed from the snapshot (doKill skips post
 *    blocks, so no notification leaves the copy) and empties the queue,
 *  - points the Jenkins URL at this host.
 */
import jenkins.model.Jenkins
import jenkins.model.JenkinsLocationConfiguration
import org.jenkinsci.plugins.workflow.flow.FlowExecutionList

def jenkins = Jenkins.get()

def cloudNames = jenkins.clouds.collect { it.name }
jenkins.clouds.clear()

def removedNodes = []
jenkins.nodes.toList().each { node ->
    removedNodes << node.nodeName
    jenkins.removeNode(node)
}

jenkins.setNumExecutors(0)
jenkins.doQuietDown()

def killed = []
FlowExecutionList.get().each { execution ->
    def run = execution.owner.executable
    run.doKill()
    killed << run.fullDisplayName
}
jenkins.queue.clear()

def location = JenkinsLocationConfiguration.get()
location.setUrl('https://rel-staging.cd.percona.com/')
location.save()

jenkins.save()
println("[a-fence] clouds cleared: ${cloudNames}; nodes removed: ${removedNodes}; numExecutors=0; quietingDown=${jenkins.isQuietingDown()}; killed: ${killed}; url=${location.getUrl()}")
