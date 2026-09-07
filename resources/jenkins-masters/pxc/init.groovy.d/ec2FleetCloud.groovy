// ec2FleetCloud.groovy  (PS-11179 / PS-11228)
//
// Registers the diversified Graviton EC2 Fleet (ASG-backed via the ec2-fleet
// plugin) as a Jenkins Cloud serving the `docker-32gb-aarch64` label on pxc.
// Uniform multi-SKU fallback path with ps3, ps80, ps57, pxb. This Fleet is the
// SOLE provider of `docker-32gb-aarch64`; the classic `min-al2023-aarch64`
// SlaveTemplate in cloud.groovy must NOT also carry that label. Co-serving
// routes the label to no cloud and starves fleet provisioning, so jobs hang on
// "all nodes of label docker-32gb-aarch64 are offline".
//
// IAM: the `Ec2FleetPluginAutoScaling` policy is attached to the
// jenkins-pxc-master role by the jenkins-arm-fleet module in
// terraform/master-pxc.tf. The plugin uses the master IAM instance profile
// (`awsCredentialsId = ""`).
//
// SSH: the `percona-jenkins` private-key credential is loaded into pxc; it
// matches the AWS key pair on the Launch Template (`key_name = "percona-jenkins"`).
// `privateIpUsed = true` because the us-west-1 master's egress does not reliably
// reach the worker's public IP; master + worker share the same VPC
// (10.156.0.0/22), so private IP routing works.
//
// Idempotent: re-applying (boot or `jenkins iac deploy`) removes the prior
// cloud instance with the same name before adding the fresh one.

import com.amazon.jenkins.ec2fleet.EC2FleetCloud
import hudson.plugins.sshslaves.SSHConnector
import hudson.plugins.sshslaves.verifiers.NonVerifyingKeyVerificationStrategy
import jenkins.model.Jenkins
import java.util.logging.Logger

final Logger LOG = Logger.getLogger('ec2FleetCloud')
final String CLOUD_NAME   = 'arm-graviton-fleet'
final String REGION       = 'us-west-1'
final String ASG_NAME     = 'jenkins-pxc-arm-graviton'
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
    true,                        // privateIpUsed -- master + worker share pxc VPC 10.156.0.0/22; public-IP SSH egress is not reliable in us-west-1
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
