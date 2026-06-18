# Decommission the ps3 EC2 spot master (re-parent its substrate)

End state: the classic EC2 `ps3` "pet" controller is gone. The spot fleet is
cancelled, the EC2 master instance is terminated, and the `module.ps3`
(`jenkins-master`) instance is deleted from Terraform. The network + worker-IAM
substrate and the ARM Graviton fleet that the master carried are re-parented into
a standalone module so the in-cluster controller keeps its `docker-32gb-aarch64`
Fleet fallback. `ps3.cd.percona.com` is served directly by the in-cluster
`jenkins-ps3-k8s` pod over the shared ALB; there is no EC2 origin and no Mode B
proxy in the path. The old EC2 `JENKINS_HOME` volume is retained, not deleted.
(PS-11206)

This is the companion to [`migrate-ps3-to-eks.md`](migrate-ps3-to-eks.md): that
runbook moved Jenkins in-cluster and kept the EC2 master fenced as a one-command
rollback; this runbook retires that fenced master once the in-cluster controller
is the proven, permanent home. Done 2026-06-07; recorded here as the validated
recipe so the pattern is mechanical if another EC2 master is retired the same way.

## Preconditions (all true before starting)

- The in-cluster `jenkins-ps3-k8s` controller permanently serves `ps3.cd` (the
  cutover Ingress wins the shared ALB rule; `curl -sI https://ps3.cd/login` is
  `200`). The proxy/reconciler entries for `ps3` are already removed (the last
  step of the migrate runbook), so the EC2 master is no longer a rollback target.
- The in-cluster `JENKINS_HOME` is independently durable: the `snapscheduler`
  addon (ADR 0028) is taking the daily CSI VolumeSnapshot and a restore drill has
  passed against the `xfs` StorageClass. The EC2 master is no longer the backup.
- The EC2 master's data EBS volume has `DeleteOnTermination=false` (verify before
  any terminate; see the gotcha below). The master EIP is already released.

## Why a ps3-scoped targeted apply, not the full saved plan

The decommission was applied with a **ps3-scoped target list**, not the repo's
normal saved-plan apply:

```sh
# Plan-only sweep of just the ps3 modules (the justfile's plan-masters style),
# reviewed, then applied with the same -target scope.
tofu plan   -target=module.ps3 -target=module.ps3_arm_fleet -out=tfplan
tofu apply  -target=module.ps3 -target=module.ps3_arm_fleet tfplan
```

A full-repo plan at that moment bundled **five unrelated drifts** that must not
ride along with an irreversible master teardown: an `argocd` Helm values change,
and four sibling arm-fleet security-group **description-only** edits. Targeting
`module.ps3` (the retiring master) and `module.ps3_arm_fleet` (its re-parent
destination) keeps the apply to exactly the decommission + the `moved{}`/`removed{}`
state re-keys, and leaves the unrelated drifts for their own change. Targeting is
a plan-only exception in this repo (`just tf-plan-masters`); the apply here is a
deliberate, reviewed use of the same scope, not a routine apply.

Apply summary: `0 add, 0 change, 32 destroy, 1 forget, 12 moved`.

## The state surgery: 12 `moved{}` + 1 `removed{}`

All of this lives in [`terraform/master-ps3.tf`](../../terraform/master-ps3.tf).
The retiring `module.ps3` block is deleted; a new
`module.ps3_arm_fleet` (`source = ./modules/jenkins-arm-standalone`) takes over
the substrate, and the cross-region peering is re-pointed at it.

- **12 `moved{}` blocks** re-key the still-load-bearing resources from
  `module.ps3.*` to `module.ps3_arm_fleet.*` with **zero diff** (a state re-key,
  not a recreate): the VPC, two subnets, IGW, route table, the internet route,
  two route-table associations, the S3 gateway endpoint, and the worker IAM role
  + role-policy + instance profile. The ARM Graviton SG / launch template / ASG
  (`jenkins-ps3-arm-graviton`) come across the same way and keep running.
- **1 `removed{}` block (`destroy = false`)** forgets the EC2 `JENKINS_HOME`
  volume `vol-06ce3f52efb4d163f` from state **without destroying it**. The volume
  carries `prevent_destroy` + `PerconaKeep=True`, so it detaches to `available`
  and survives both the apply and the daily `percona-dev-admin` volume-cleanup
  Lambda (which only reaps `available` volumes missing that tag).
- Everything else in `module.ps3` (spot fleet request, launch template, master
  IAM role + SGs, init bucket, SQS) is master-only and is **destroyed** when the
  old module block is gone.

The retained home is preserved three ways: (1) the retained `available` volume,
(2) a full pre-decommission EBS snapshot (`snap-07e2b31bc3c01241a`, 2026-06-07),
and (3) an S3 archive bucket (`jenkins-ps3-home-archive`, eu-west-1, SSE-KMS, BPA,
~227k objects). The master-role autoscaling IAM policy was also dropped: the
in-cluster controller drives the EC2 Fleet through its EKS Pod Identity role, not
a master-role policy.

## The orphan-spot-fleet gotcha (the one that bites)

**Destroying `aws_spot_fleet_request` does NOT terminate the instance it
launched.** Cancelling the fleet request (even with the API's terminate flag)
left the EC2 master `i-0ebc45a55ec7098d7` running. The follow-on consequence:
the master security-group destroy then **hung ~6 minutes** on a lingering ENI
that the still-running instance held, because AWS will not delete an SG while a
network interface still references it.

Resolution — terminate the instance by hand, but **only after confirming the
data volume will survive it**:

```sh
# 1. Confirm the data volume detaches, not deletes, on terminate.
AWS_PROFILE=percona-dev-admin aws ec2 describe-instances --region eu-west-1 \
  --instance-ids i-0ebc45a55ec7098d7 \
  --query 'Reservations[].Instances[].BlockDeviceMappings[?DeviceName==`/dev/xvdj`].Ebs.[VolumeId,DeleteOnTermination]'
# Expect: vol-06ce... , False   (DeleteOnTermination=false on the data volume)

# 2. Only then terminate. The lingering ENI clears, the SG destroy unblocks.
AWS_PROFILE=percona-dev-admin aws ec2 terminate-instances --region eu-west-1 \
  --instance-ids i-0ebc45a55ec7098d7
```

Lesson: when retiring a spot-fleet-backed master, expect to terminate the
instance yourself, and gate that terminate on a `DeleteOnTermination=false`
check of the data volume so a teardown can never delete the real home.

## Post-apply gates (all must pass)

```sh
# Data volume retained and detached (not deleted, not in-use).
AWS_PROFILE=percona-dev-admin aws ec2 describe-volumes --region eu-west-1 \
  --volume-ids vol-06ce3f52efb4d163f \
  --query 'Volumes[0].[State,Tags[?Key==`PerconaKeep`].Value|[0]]'
# Expect: available , True

# The ARM Graviton ASG survived the re-parent (still desired/min/max intact).
AWS_PROFILE=percona-dev-admin aws autoscaling describe-auto-scaling-groups \
  --region eu-west-1 --auto-scaling-group-names jenkins-ps3-arm-graviton \
  --query 'AutoScalingGroups[0].[AutoScalingGroupName,MinSize,MaxSize]'

# ps3.cd is served by the in-cluster pod (Jenkins headers, not the degraded page).
curl -sI https://ps3.cd.percona.com/login | grep -iE 'HTTP/|x-jenkins'
# Expect: HTTP/2 200 and an x-jenkins: header.

# The EC2 master is gone.
AWS_PROFILE=percona-dev-admin aws ec2 describe-instances --region eu-west-1 \
  --instance-ids i-0ebc45a55ec7098d7 \
  --query 'Reservations[].Instances[].State.Name'
# Expect: terminated
```

## Remaining cleanup (follow-ups, not blockers)

- **DLM policy `policy-03d5a16518cf69a06`** ("ps3 DATA - do not remove") is still
  `ENABLED` and is now snapshotting the **dead, detached** data volume on its
  schedule. It is a disable candidate: the volume is already preserved by the
  one-off snapshot + the S3 archive, so the daily DLM snapshots add cost for no
  benefit now the master is gone. Disable (do not delete the existing snapshots)
  once the retained-home story is signed off. For general orphan-snapshot
  hygiene on other retired masters, see
  [`orphaned-snapshot-cleanup.md`](orphaned-snapshot-cleanup.md); the
  `eu-west-1` ps3 snapshots are out of scope there until this DLM policy is
  retired.
- **Stale chart-default orphan PVC.** A 1.3 GB `jenkins-ps3-k8s` PVC backed by
  `vol-00591943` (the Jenkins chart's default dynamic claim from an early boot,
  never the real home) is orphaned and can be deleted with its PV. This is the
  chart-default volume, **not** the real in-cluster home (`ps3-k8s-jenkins-home`,
  `vol-0ad6c44c2b72508af`) and **not** the retained EC2 home (`vol-06ce...`);
  confirm the volume id before deleting.
- **Re-parent scaffolding.** The 12 `moved{}` + 1 `removed{}` blocks in
  `terraform/master-ps3.tf` are one-time state-migration scaffolding. They are
  inert once applied and can be deleted in a follow-up commit (a no-op plan
  confirms they have done their job).

## See also

- [`migrate-ps3-to-eks.md`](migrate-ps3-to-eks.md) — the in-cluster migration this
  retirement completes (fence → prove → decommission).
- [`adr/0028-jenkins-dynamic-config-data-lifecycle.md`](../adr/0028-jenkins-dynamic-config-data-lifecycle.md)
  — the in-cluster `JENKINS_HOME` backup (snapscheduler) that replaces the EC2
  master as the durability story.
- [`terraform/modules/jenkins-arm-standalone/`](../../terraform/modules/jenkins-arm-standalone/)
  — the module the substrate + ARM fleet were re-parented into.
