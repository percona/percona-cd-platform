/**
 * pmm-staging stub. Same filename as production pmm's dockerFarmCloud.groovy, so the boot-time
 * S3 fetch and the 30-minute init sync overwrite the copy restored inside
 * JENKINS_HOME. It re-asserts the removal of the production Docker Farm EC2 cloud in case anything re-adds one between syncs.
 */
import jenkins.model.Jenkins

def jenkins = Jenkins.get()
def gone = jenkins.clouds.findAll { it.getClass().getName().contains('EC2Cloud') }
gone.each { jenkins.clouds.remove(it) }
if (!gone.isEmpty()) {
    jenkins.save()
}
println("[dockerFarmCloud.groovy fence] removed ${gone.size()} EC2 clouds")
