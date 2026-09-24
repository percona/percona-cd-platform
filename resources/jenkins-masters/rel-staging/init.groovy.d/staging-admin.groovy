/**
 * rel-staging local login. GitHub OAuth is bound to the production
 * hostname, so the copy uses Jenkins' own user database with one admin,
 * staging-admin, whose password is the SSM SecureString below
 * (terraform/staging-admin.tf). Runs every boot, so a rotated password applies
 * on the next restart. The restored authorization matrix stays as it is and
 * staging-admin is added to it.
 */
import jenkins.model.Jenkins
import hudson.security.HudsonPrivateSecurityRealm
import hudson.security.GlobalMatrixAuthorizationStrategy
import org.jenkinsci.plugins.matrixauth.PermissionEntry

def parameter = '/percona-ci-platform/jenkins-staging/rel/admin-password'
def command = ['aws', 'ssm', 'get-parameter', '--region', 'us-east-1', '--name', parameter,
               '--with-decryption', '--query', 'Parameter.Value', '--output', 'text']
def process = command.execute()
def out = new StringBuilder()
def err = new StringBuilder()
process.waitForProcessOutput(out, err)
if (process.exitValue() != 0) {
    println("[staging-admin] could not read ${parameter}: ${err.toString().trim()}")
    return
}
def password = out.toString().trim()

def jenkins = Jenkins.get()
def realm = jenkins.securityRealm instanceof HudsonPrivateSecurityRealm ? jenkins.securityRealm : new HudsonPrivateSecurityRealm(false)
realm.createAccount('staging-admin', password)
def authorization = jenkins.authorizationStrategy
if (authorization instanceof GlobalMatrixAuthorizationStrategy) {
    authorization.add(Jenkins.ADMINISTER, PermissionEntry.user('staging-admin'))
}
jenkins.setSecurityRealm(realm)
jenkins.save()
println("[staging-admin] local login staging-admin set from ${parameter}")
