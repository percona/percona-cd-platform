// ec2FleetCloud.groovy  (PS-11179)
//
// Registers the diversified Graviton EC2 Fleet (ASG-backed via the ec2-fleet
// plugin) as a Jenkins Cloud serving the `docker-32gb-aarch64` label on pxb.
// Uniform multi-SKU fallback path with ps3/ps57/ps80. pxb's classic EC2
// docker-32gb-aarch64 template (c7g.4xlarge) is retired in cloud.groovy so this
// Fleet is the sole AWS provider of the label (no double-provider); Hetzner CAX
// remains the primary arm64 path via htz.cloud.groovy.
//
// IAM: the `Ec2FleetPluginAutoScaling` policy is attached to the pxb master role
// by the standalone TF block in terraform/master-pxb.tf (jenkins-arm-fleet
// module, module.pxb_arm_fleet). The plugin uses the master IAM instance profile
// (`awsCredentialsId = ""`).
//
// SSH: the `percona-jenkins` private-key credential is loaded into pxb; it matches
// the AWS key pair on the Launch Template (`key_name = "percona-jenkins"`).
// `privateIpUsed = true` because the us-west-2 master's egress does not reliably
// reach the worker's public IP; master + worker share the same VPC
// (10.179.0.0/22), so private IP routing works.
//
// Idempotent: re-applying via `jenkins iac deploy` removes the prior cloud
// instance with the same name before adding the fresh one.

import com.amazon.jenkins.ec2fleet.EC2FleetCloud
import hudson.plugins.sshslaves.SSHConnector
import hudson.plugins.sshslaves.verifiers.NonVerifyingKeyVerificationStrategy
import jenkins.model.Jenkins
import java.util.logging.Logger

final Logger LOG = Logger.getLogger('ec2FleetCloud')
final String CLOUD_NAME   = 'arm-graviton-fleet'
final String REGION       = 'us-west-2'
final String ASG_NAME     = 'jenkins-pxb-arm-graviton'
final String LABEL        = 'docker-32gb-aarch64'
final String SSH_CRED_ID  = 'percona-jenkins'

Jenkins.instance.clouds.findAll { it.name == CLOUD_NAME }.each {
    Jenkins.instance.clouds.remove(it)
}

def sshConn = new SSHConnector(
    22, SSH_CRED_ID,
    '', '', '', '',
    null, null, null,
    new NonVerifyingKeyVerificationStrategy()
)

def fleet = new EC2FleetCloud(
    CLOUD_NAME,
    '',                          // awsCredentialsId (empty -> master IAM instance profile)
    '',                          // credentialsId (legacy field, kept empty)
    REGION,
    '',                          // endpoint
    ASG_NAME,
    LABEL,
    '/mnt/jenkins',              // fsRoot
    sshConn,                     // computerConnector
    true,                        // privateIpUsed -- master + worker share the master VPC; public-IP SSH egress is not reliable in us-west-2
    false,                       // alwaysReconnect
    (Integer) 10,                // idleMinutes (NON-zero: 0 = never scale down)
    0,                           // minSize
    16,                          // maxSize (matches TF)
    0,                           // minSpareSize
    1,                           // numExecutors
    true,                        // addNodeOnlyIfRunning
    false,                       // restrictUsage
    '-1',                        // maxTotalUses (-1 = unlimited)
    true,                        // disableTaskResubmit (PS-11265: resubmit aborts pipeline node bodies and re-schedules with wrong params and no cause)
    (Integer) 600,               // initOnlineTimeoutSec
    (Integer) 15,                // initOnlineCheckIntervalSec
    (Integer) 10,                // cloudStatusIntervalSec
    false,                       // noDelayProvision
    false,                       // scaleExecutorsByWeight
    new EC2FleetCloud.NoScaler()
)

Jenkins.instance.clouds.add(fleet)
LOG.info("ec2FleetCloud: registered cloud='${CLOUD_NAME}' label='${LABEL}' fleet='${ASG_NAME}' region='${REGION}'")
