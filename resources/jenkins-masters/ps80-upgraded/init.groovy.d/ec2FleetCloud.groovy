/**
 * ps80-upgraded clone fence stub. Same filename as production ps80's
 * ec2FleetCloud.groovy, so the boot-time S3 fetch and the 30-minute init sync overwrite
 * the copy inherited inside the restored JENKINS_HOME and the production
 * behavior below can never run on the clone (re-attaching to ASG jenkins-ps80-arm-graviton, the SAME live ASG production ps80 drives). a-fence.groovy has
 * already cleared Jenkins.clouds this boot; this stub re-asserts the
 * removal in case anything re-adds a matching cloud between syncs.
 */
import jenkins.model.Jenkins

def jenkins = Jenkins.get()
def gone = jenkins.clouds.findAll { it.getClass().getName().contains('EC2FleetCloud') }
gone.each { jenkins.clouds.remove(it) }
if (!gone.isEmpty()) {
    jenkins.save()
}
println("[ec2FleetCloud.groovy fence] removed ${gone.size()} EC2 Fleet clouds")
