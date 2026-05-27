// Persistent: runs every startup.
// Sets Jenkins URL from JENKINS_URL env var.
import jenkins.model.*

def newUrl = System.getenv("JENKINS_URL")
if (newUrl) {
  if (!newUrl.endsWith("/")) newUrl += "/"
  def loc = JenkinsLocationConfiguration.get()
  loc.url = newUrl
  loc.save()
  println "Jenkins URL set to: ${newUrl}"
} else {
  println "JENKINS_URL env var not set, keeping existing URL"
}
