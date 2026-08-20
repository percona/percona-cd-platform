/**
 * pxb-upgraded clone fence. Runs FIRST (init.groovy.d is alphabetical) on
 * every boot. The JENKINS_HOME on this master is restored from a snapshot of
 * production pxb, so at JVM start config.xml still carries prod's EC2 and
 * Hetzner clouds and nodes/ carries prod's live agent records. Everything
 * here must complete before any cloud or agent machinery acts:
 *
 *  - clear ALL clouds, so no provisioning and no orphan/retention reaping
 *    against production workers is possible,
 *  - remove every inherited agent, so SSH launchers never reconnect to
 *    production worker IPs and idle retention never terminates them,
 *  - built-in executors to 0, so nothing can build on the controller,
 *  - Jenkins URL to this host, so nothing advertises pxb.cd.percona.com.
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
location.setUrl('https://pxb-upgraded.cd.percona.com/')
location.save()

jenkins.save()
println("[a-fence] clouds cleared: ${cloudNames}; nodes removed: ${removedNodes}; numExecutors=0; url=${location.getUrl()}")
