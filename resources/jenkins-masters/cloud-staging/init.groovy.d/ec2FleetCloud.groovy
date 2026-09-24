/**
 * cloud-staging stub. Same filename as production cloud's ec2FleetCloud.groovy, so the boot-time
 * S3 fetch and the 30-minute init sync overwrite the copy restored inside
 * JENKINS_HOME. It re-asserts the removal of the EC2 Fleet cloud on ASG jenkins-cloud-arm-graviton, the same live ASG production cloud drives in case anything re-adds one between syncs.
 */
import jenkins.model.Jenkins

def jenkins = Jenkins.get()
def gone = jenkins.clouds.findAll { it.getClass().getName().contains('EC2FleetCloud') }
gone.each { jenkins.clouds.remove(it) }
if (!gone.isEmpty()) {
    jenkins.save()
}
println("[ec2FleetCloud.groovy fence] removed ${gone.size()} EC2 Fleet clouds")
