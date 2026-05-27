// Persistent: runs every startup AFTER cloud.groovy (alphabetical: 'e' > 'c')
// Switches EC2 clouds to use DefaultCredentialsProvider so the patched
// EC2 plugin (with STS dependency) picks up IRSA on EKS pods.
//
// Requires: ec2 plugin with fix/eks-irsa-support patches.
import jenkins.model.Jenkins
import hudson.plugins.ec2.EC2Cloud

Jenkins.instance.clouds.findAll { it instanceof EC2Cloud }.each { cloud ->
  def field = EC2Cloud.getDeclaredField("useInstanceProfileForCredentials")
  field.accessible = true
  field.set(cloud, false)
  println "EC2 IRSA: ${cloud.name} -> useInstanceProfile=false"
}
Jenkins.instance.save()
