/**
 * rel-staging stub. Same filename as production rel's htz.cloud.groovy, so the boot-time
 * S3 fetch and the 30-minute init sync overwrite the copy restored inside
 * JENKINS_HOME. It re-asserts the removal of the Hetzner cloud, whose inherited API token could enumerate and reap production rel's live workers in case anything re-adds one between syncs.
 */
import jenkins.model.Jenkins

def jenkins = Jenkins.get()
def gone = jenkins.clouds.findAll { it.getClass().getName().contains('HetznerCloud') }
gone.each { jenkins.clouds.remove(it) }
if (!gone.isEmpty()) {
    jenkins.save()
}
println("[htz.cloud.groovy fence] removed ${gone.size()} Hetzner clouds")
