// Persistent: runs every startup AFTER cloud.groovy (alphabetical: 'f' > 'c')
// Switches EC2 worker connection strategy from PUBLIC_DNS to PRIVATE_IP.
// Required when Jenkins runs in a different VPC (EKS) from workers,
// connected via VPC peering (which only routes private IPs).
import jenkins.model.Jenkins
import hudson.plugins.ec2.EC2Cloud
import hudson.plugins.ec2.ConnectionStrategy

def count = 0
Jenkins.instance.clouds.findAll { it instanceof EC2Cloud }.each { cloud ->
  cloud.templates.each { t ->
    def field = t.class.getDeclaredField("connectionStrategy")
    field.accessible = true
    if (field.get(t) != ConnectionStrategy.PRIVATE_IP) {
      field.set(t, ConnectionStrategy.PRIVATE_IP)
      count++
    }
  }
}
if (count > 0) {
  Jenkins.instance.save()
  println "Connection strategy: set ${count} templates to PRIVATE_IP"
} else {
  println "Connection strategy: all templates already PRIVATE_IP"
}
