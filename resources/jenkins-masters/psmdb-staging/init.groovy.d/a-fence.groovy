/**
 * psmdb-staging fence. Runs FIRST (init.groovy.d is alphabetical) on every boot.
 * JENKINS_HOME is restored from a snapshot of production psmdb, so at JVM start
 * config.xml still carries production's clouds and nodes/ carries its live
 * agent records. This script:
 *
 *  - clears ALL clouds, so nothing provisions or reaps production workers,
 *  - removes every inherited agent, so no launcher reconnects to a production
 *    worker and no idle retention terminates one,
 *  - sets built-in executors to 0, so nothing builds on the controller,
 *  - points the Jenkins URL at this host.
 */
import jenkins.model.Jenkins
import jenkins.model.JenkinsLocationConfiguration

def jenkins = Jenkins.get()

def cloudNames = jenkins.clouds.collect { it.name }
jenkins.clouds.clear()

def removedNodes = []
jenkins.nodes.toList().each { node ->
    removedNodes << node.nodeName
    jenkins.removeNode(node)
}

jenkins.setNumExecutors(0)

def location = JenkinsLocationConfiguration.get()
location.setUrl('https://psmdb-staging.cd.percona.com/')
location.save()

jenkins.save()
println("[a-fence] clouds cleared: ${cloudNames}; nodes removed: ${removedNodes}; numExecutors=0; url=${location.getUrl()}")
