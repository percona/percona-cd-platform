// Reloads the AWS_STASH credential on the in-cluster ps3 controller from AWS
// Secrets Manager. Run it from the Script Console after a key rotation, see
// docs/runbooks/ps3-aws-stash.md. The pod's Pod Identity session authenticates
// the read, the key pair never leaves the JVM, and the output prints only the
// access key id.

import com.cloudbees.jenkins.plugins.awscredentials.AWSCredentialsImpl
import com.cloudbees.plugins.credentials.CredentialsScope
import com.cloudbees.plugins.credentials.SystemCredentialsProvider
import com.cloudbees.plugins.credentials.domains.Domain
import groovy.json.JsonSlurper

def secretId = 'percona-ci-platform/jenkins/ps3/aws-stash'
def credentialId = 'AWS_STASH'
def region = 'us-east-1'

// The SDK classes come from the aws-java-sdk-secretsmanager plugin, so they are
// loaded through the plugin classloader instead of a static import.
def loader = Jenkins.instance.pluginManager.uberClassLoader
def client = Class.forName('com.amazonaws.services.secretsmanager.AWSSecretsManagerClientBuilder', true, loader)
    .standard().withRegion(region).build()
def request = Class.forName('com.amazonaws.services.secretsmanager.model.GetSecretValueRequest', true, loader)
    .newInstance().withSecretId(secretId)
def pair = new JsonSlurper().parseText(client.getSecretValue(request).secretString)
assert pair.access_key_id && pair.secret_access_key : "${secretId} lacks access_key_id or secret_access_key"

def store = SystemCredentialsProvider.instance.store
def domain = Domain.global()
def existing = store.getCredentials(domain).find { it.id == credentialId }
def description = existing?.description ?: 'credentials for stash build artifacts'
def fresh = new AWSCredentialsImpl(CredentialsScope.GLOBAL, credentialId, pair.access_key_id, pair.secret_access_key, description)
def accepted = existing ? store.updateCredentials(domain, existing, fresh) : store.addCredentials(domain, fresh)
assert accepted : "credential store rejected ${credentialId}"

def current = store.getCredentials(domain).find { it.id == credentialId }
println "${credentialId} ${existing ? 'updated' : 'created'}, access key id ${current.accessKey}"
