// ec2FleetCloud.groovy  (PS-11179)
//
// Registers the diversified EC2 Fleet clouds (ASG-backed via the ec2-fleet
// plugin) on ps80:
//   - arm-graviton-fleet: serves `docker-32gb-aarch64`, the AWS Graviton
//     fallback when Hetzner ARM (CAX) capacity is unavailable.
//   - x86-bookworm-fleet: serves `min-bookworm-x64` (molecule package-test
//     drivers), replacing the classic single-pool m6a.2xlarge spot template
//     whose one (type x AZ) pool was a reclaim blast-radius. The classic
//     template registration is removed from cloud.groovy in the same change:
//     two clouds serving one label starve provisioning.
//   - x86-docker-fleet: serves `docker` (general ps80 CI workers) for the
//     same reason; its classic template is retired the same way.
//
// IAM: each fleet's `Ec2FleetPluginAutoScaling` policy is attached to the
// ps80 master role by its TF module in terraform/master-ps80.tf
// (module.ps80_arm_fleet, module.ps80_x86_fleet). The plugin uses the master
// IAM instance profile (`awsCredentialsId = ""`).
//
// SSH: the `percona-jenkins` private-key credential is loaded into ps80; it
// matches the AWS key pair on both Launch Templates (`key_name =
// "percona-jenkins"`) and logs in as ec2-user (the bookworm workers create
// that user in their launch-template user_data).
// `privateIpUsed = true` because the us-west-2 master's egress does not reliably
// reach the worker's public IP; master + workers share the same VPC
// (10.155.0.0/22), so private IP routing works.
//
// Idempotent: re-applying via `jenkins iac deploy` removes the prior cloud
// instance with the same name before adding the fresh one.

import com.amazon.jenkins.ec2fleet.EC2FleetCloud
import hudson.plugins.sshslaves.SSHConnector
import hudson.plugins.sshslaves.verifiers.NonVerifyingKeyVerificationStrategy
import jenkins.model.Jenkins
import java.util.logging.Level
import java.util.logging.Logger

final Logger LOG = Logger.getLogger('ec2FleetCloud')
final String REGION       = 'us-west-2'
final String SSH_CRED_ID  = 'percona-jenkins'

// One entry per fleet. restrictUsage mirrors the classic template's
// Node.Mode (docker-32gb-aarch64 and docker were NORMAL, min-bookworm-x64
// EXCLUSIVE).
final List<Map> FLEETS = [
    [
        name       : 'arm-graviton-fleet',
        asg        : 'jenkins-ps80-arm-graviton',
        label      : 'docker-32gb-aarch64',
        idleMinutes: 10,
        maxSize    : 16,
        restrict   : false,
    ],
    [
        name       : 'x86-bookworm-fleet',
        asg        : 'jenkins-ps80-x86-bookworm',
        label      : 'min-bookworm-x64',
        idleMinutes: 15,
        maxSize    : 24,
        restrict   : true,
    ],
    [
        name       : 'x86-docker-fleet',
        asg        : 'jenkins-ps80-x86-docker',
        label      : 'docker',
        idleMinutes: 15,
        maxSize    : 32,
        restrict   : false,
    ],
]

// Per-fleet try/catch: the EC2FleetCloud constructor validates its ASG against
// live AWS, so a missing ASG (e.g. groovy deployed before the tofu apply that
// creates it) must not abort the whole script and take the sibling fleets
// down with it. Construct-then-swap: the new cloud is built and validated
// BEFORE the registered one is removed, so a failed construction keeps the
// last working cloud. Failures are collected and re-thrown at the end so a
// hot deploy reports them loudly.
List<String> failedFleets = []

FLEETS.each { Map f ->
    try {
    def sshConn = new SSHConnector(
        22, SSH_CRED_ID,
        '', '', '', '',
        null, null, null,
        new NonVerifyingKeyVerificationStrategy()
    )

    def fleet = new EC2FleetCloud(
        (String) f.name,
        '',                          // awsCredentialsId (empty -> master IAM instance profile)
        '',                          // credentialsId (legacy field, kept empty)
        REGION,
        '',                          // endpoint
        (String) f.asg,
        (String) f.label,
        '/mnt/jenkins',              // fsRoot
        sshConn,                     // computerConnector
        true,                        // privateIpUsed -- master + worker share the master VPC; public-IP SSH egress is not reliable in us-west-2
        false,                       // alwaysReconnect
        (Integer) f.idleMinutes,     // idleMinutes (NON-zero: 0 = never scale down)
        0,                           // minSize
        (Integer) f.maxSize,         // maxSize (matches TF)
        0,                           // minSpareSize
        1,                           // numExecutors
        true,                        // addNodeOnlyIfRunning
        (boolean) f.restrict,        // restrictUsage
        '-1',                        // maxTotalUses (-1 = unlimited)
        true,                        // disableTaskResubmit (PS-11265: resubmit aborts pipeline node bodies and re-schedules with wrong params and no cause)
        (Integer) 600,               // initOnlineTimeoutSec
        (Integer) 15,                // initOnlineCheckIntervalSec
        (Integer) 10,                // cloudStatusIntervalSec
        false,                       // noDelayProvision
        false,                       // scaleExecutorsByWeight
        new EC2FleetCloud.NoScaler()
    )

    Jenkins.instance.clouds.findAll { it.name == (String) f.name }.each {
        Jenkins.instance.clouds.remove(it)
    }

    Jenkins.instance.clouds.add(fleet)
    LOG.info("ec2FleetCloud: registered cloud='${f.name}' label='${f.label}' fleet='${f.asg}' region='${REGION}'")
    } catch (Exception e) {
        failedFleets << (String) f.name
        LOG.log(Level.SEVERE, "ec2FleetCloud: FAILED to register cloud='${f.name}' fleet='${f.asg}', keeping the previously registered cloud if any", e)
    }
}

if (failedFleets) {
    throw new IllegalStateException("ec2FleetCloud: fleet registration failed for: ${failedFleets.join(', ')}")
}
