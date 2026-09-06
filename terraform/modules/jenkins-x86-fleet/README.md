# jenkins-x86-fleet

Diversified x86_64 EC2 spot pool consumed by the Jenkins **ec2-fleet plugin**
(`EC2FleetCloud`). Replaces a classic ec2-plugin `SlaveTemplate` whose single
(instance type x AZ) spot pool is a reclaim blast-radius: one pool squeeze
takes out every worker of the label at once.

## Design decisions

- **Sibling of `jenkins-arm-fleet`, kept separate.** The bootstrap contract
  differs (distribution userland, package manager, SSH login shim) and the arm
  module's resource names are state contract surfaces that must not churn.
- **ASG + `MixedInstancesPolicy`, `price-capacity-optimized`.** AWS picks, per
  instance, the deepest of (types x subnets) pools. Measured on the ps80
  min-bookworm pool: Spot Placement Score 1 (single m6a.2xlarge / one AZ)
  vs 9 (eight m-family types x two AZs) at the same 22-unit target.
- **AMI + user_data are caller-supplied.** The OS userland is part of the
  label contract (a `min-bookworm-x64` job expects Debian 12 apt userland),
  so the pool pins the same AMI as the classic template it replaces.
  `bookworm-worker-user-data.sh` in this module is the Debian 12 profile;
  other userlands bring their own file.
- **`ec2-user` login shim.** The fleet-wide `percona-jenkins` Jenkins
  credential authenticates as `ec2-user`; Debian cloud images only create
  `admin`. The user_data creates `ec2-user` with the same key material and
  sudo grant instead of forking the credential per distribution.
- **One label, one cloud.** The classic template registration for the label
  must be removed in the same change that points `EC2FleetCloud` at this ASG;
  two clouds serving one label starve provisioning (both defer to the other's
  planned capacity).

## Wiring

Feed `asg_name` to the master's `init.groovy.d/ec2FleetCloud.groovy`
(`EC2FleetCloud` `fleet` field) and keep `disableTaskResubmit: true`
(see repo CLAUDE.md gotcha 15 / PS-11265).
