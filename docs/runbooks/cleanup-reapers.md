# Operating the cleanup reapers (volume + EC2 + snapshot)

Three scheduled AWS Lambda reapers keep the `percona-dev-admin` account tidy by
deleting resources that lack the mandatory cleanup tags or have aged past their
retention. This runbook is how an operator deploys them, reads their dry-run
decisions, arms them for live deletion, protects resources from them, and
pauses or rolls them back.

Every reaper ships `DRY_RUN="true"`: it logs every WOULD-delete decision but
deletes nothing until deliberately armed. Arming is a reviewed code change,
never a one-off CLI flag. Current state: volume and EC2 are armed, the
snapshot reaper is still dry-run.

The snapshot reaper covers ONLY the ebs-csi (snapscheduler) snapshot class.
Orphaned EBS snapshots left by CloudFormation `DeletionPolicy: Snapshot` are
still cleaned up by hand; see
[`orphaned-snapshot-cleanup.md`](orphaned-snapshot-cleanup.md).

## What runs

| Reaper | Function | Schedule | Sweeps | Targets |
|--------|----------|----------|--------|---------|
| Volume | `percona-ci-platform-volume-cleanup` | `rate(1 day)` | A fixed 8-region list in code | `available` (unattached) EBS volumes missing the cleanup opt-outs |
| EC2 | `percona-ci-platform-ec2-cleanup` | `rate(5 minutes)` | All regions via `describe_regions()` | Untagged EC2 instances; orphan `eksctl-<cluster>-cluster` stacks |
| Snapshot | `percona-ci-platform-snapshot-cleanup` | `rate(1 day)` | `us-east-1` (the cluster region) | `managed-by=ebs-csi-driver` EBS snapshots outside the newest 14 per volume and older than 14 days |

All three deploy as a single function in `us-east-1` (the default provider
region); the handlers iterate regions in code, so no IAM policy carries an
`aws:RequestedRegion` condition. Each function logs to
`/aws/lambda/<function-name>`. Reserved concurrency is `1`, so a slow run can
never overlap itself.

The snapshot reaper exists because the in-cluster retention cannot delete
physical snapshots: snapscheduler prunes only the `VolumeSnapshot` objects, and
the `ebs-csi-retain` class keeps the EBS snapshot on purpose (a GitOps prune
must not erase recovery points). Its blast radius is enforced twice: the
handler selects only `managed-by=ebs-csi-driver` snapshots, and the
`ec2:DeleteSnapshot` IAM grant carries the same tag as a `StringEquals`
condition, so DLM, AWS Backup, AMI and CloudFormation snapshots are
unreachable even under a handler bug.

The wiring (Lambda, EventBridge rule, log group, execution role) lives in the
`scheduled-lambda` module; see
[`terraform/modules/scheduled-lambda/README.md`](../../terraform/modules/scheduled-lambda/README.md).
The per-reaper policy and instantiation live in
[`terraform/volume-cleanup.tf`](../../terraform/volume-cleanup.tf),
[`terraform/ec2-cleanup.tf`](../../terraform/ec2-cleanup.tf)
and [`terraform/snapshot-cleanup.tf`](../../terraform/snapshot-cleanup.tf).
All tunables (schedules, `dry_run`, EKS skip regex, age floors) live in the
`Cleanup Lambda parameters` block of
[`terraform/locals.tf`](../../terraform/locals.tf).

## First deploy and dry-run bake-in

Export `AWS_PROFILE` first (`export AWS_PROFILE=percona-dev-admin`). Terraform
goes only through the `just tf-*` recipes; never raw `tofu`, never a CLI `-var`.

### Step 1 -- plan and apply

```sh
just tf-plan
# Writes terraform/tfplan. Review it before applying.
just tf-apply
# Applies the saved tfplan. Never auto-approve.
```

Note: the plan may carry unrelated in-place AMI bumps that come from the shared
`amis.tf` SSM data source reading a newer AL2023 build. That drift is routine and
not part of this change; confirm it is only those AMI attributes before you
apply.

### Step 2 -- review the dry-run decisions in CloudWatch Logs

A reaper in dry-run logs WOULD-delete decisions and deletes nothing (today:
the snapshot reaper; volume and EC2 are already armed). Tail each log group
and confirm the decisions look right before arming.

```sh
just lambda-logs ec2-cleanup        # wraps: aws logs tail /aws/lambda/percona-ci-platform-ec2-cleanup
just lambda-logs volume-cleanup     # wraps: aws logs tail /aws/lambda/percona-ci-platform-volume-cleanup
just lambda-logs snapshot-cleanup   # wraps: aws logs tail /aws/lambda/percona-ci-platform-snapshot-cleanup
```

The EC2 reaper runs every 5 minutes, so a window appears quickly. The volume
and snapshot reapers run once a day; invoke them by hand to see a cycle now.

### Step 3 -- invoke by hand and read the structured return

```sh
aws lambda invoke --function-name percona-ci-platform-volume-cleanup /tmp/out.json && cat /tmp/out.json
```

The volume reaper returns `{deleted, skipped, dry_run}`. Each `deleted` row is
`(region, volume_id, size_gib)`. In dry-run (`dry_run: true`) those rows are the
volumes it WOULD delete, not volumes it deleted; each `skipped` row is
`(region, volume_id, reason)`.

The EC2 reaper returns `{terminated, deleted_clusters, skipped, dry_run}`. In
dry-run, `terminated` and `deleted_clusters` are what it WOULD have terminated or
torn down.

```sh
aws lambda invoke --function-name percona-ci-platform-ec2-cleanup /tmp/out.json && cat /tmp/out.json
```

The snapshot reaper returns `{deleted, skipped, dry_run}`. Each `deleted` row is
`(region, snapshot_id, volume_id, size_gib)`; each `skipped` row is
`(region, snapshot_id, reason)`, where reason `newest-14` marks the retained
window, `PerconaKeep` marks the pre-reaper backlog (see the untag step below),
and `younger-than-14d` marks the retention floor.

```sh
aws lambda invoke --function-name percona-ci-platform-snapshot-cleanup /tmp/out.json && cat /tmp/out.json
```

Let each reaper bake in dry-run long enough to trust the decisions (one full
daily cycle for volume and snapshot, several 5-minute cycles for EC2) before
arming.

### Snapshot reaper only: untag the pre-reaper backlog

Snapshots created before the reaper existed carry `PerconaKeep=True` from the
old `ebs-csi-retain` tagSpecification, so the reaper skips every one of them
(`skipped` reason `PerconaKeep`). After the dry-run review, remove that tag from
the CSI class snapshots so they age out; this is the only by-hand step:

```sh
AWS_PROFILE=percona-dev-admin aws ec2 describe-snapshots --region us-east-1 \
  --owner-ids self \
  --filters Name=tag:managed-by,Values=ebs-csi-driver Name=tag:PerconaKeep,Values=True \
  --query 'Snapshots[].SnapshotId' --output text | tr '\t' '\n' > /tmp/csi-snaps.txt
wc -l /tmp/csi-snaps.txt   # expect the backlog count from the dry-run skips
xargs -a /tmp/csi-snaps.txt -n 50 aws ec2 delete-tags --region us-east-1 \
  --tags Key=PerconaKeep --resources
```

The next dry-run cycle then reports them under `deleted` (WOULD delete); review
that list before arming.

## Arming a reaper

Arming flips `dry_run` to `"false"` so the reaper deletes for real. Edit the
`Cleanup Lambda parameters` block of
[`terraform/locals.tf`](../../terraform/locals.tf) in a reviewed PR. Never arm
with a CLI `-var`; the committed value is the audit trail.

```hcl
volume_cleanup = {
  schedule      = "rate(1 day)"
  dry_run       = "false" # was "true"
  min_age_hours = "24"
}
```

A contract test gates this on purpose. `test_dry_run_locked_to_committed_state`
in
[`terraform/lambdas/tests/test_terraform_contract.py`](../../terraform/lambdas/tests/test_terraform_contract.py)
asserts that each reaper's committed `dry_run` matches the reviewed
expectation (volume and EC2 armed `"false"`, snapshot `"true"` until its
bake-in review). The lock works in both directions; arming is meant to be a
deliberate, reviewed change rather than a value that can drift unnoticed.

Because of that test, arming is a single PR that does two things together, by
design:

1. Edit `dry_run = "false"` in the relevant `locals.tf` block.
2. Update the `test_dry_run_locked_to_committed_state` expectation for that
   block in the same PR, so the test and the live state agree.

Editing only `locals.tf` turns the test red and blocks the merge; that red is the
review gate working. Then plan, apply, and verify one real cycle in the logs:

```sh
just tf-plan
just tf-apply
just lambda-logs volume-cleanup     # wraps: aws logs tail /aws/lambda/percona-ci-platform-volume-cleanup
# Confirm the log now shows real deletions, not DRY_RUN lines.
```

## Protecting resources (opt-outs)

Tag or name a resource so a reaper spares it.

Volume reaper. A volume is spared when any of these hold:

- It carries a `PerconaKeep` tag (capital P, capital K).
- Its `Name` tag contains `do not remove` (case-insensitive).
- It is younger than 24h (the `min_age_hours` floor), so a volume created during
  a running build is never reaped mid-build.

Only `available` (unattached) volumes are ever candidates; an in-use volume is
never touched.

Snapshot reaper. A snapshot is spared when any of these hold:

- It does not carry `managed-by=ebs-csi-driver` (out of scope entirely; the IAM
  condition enforces the same boundary a second time).
- It is among the newest `keep_count` (default 14) snapshots of its source
  volume, regardless of age.
- It is younger than `retention_days` (default 14).
- It carries a `PerconaKeep` tag (capital P, capital K).
- Its `Name` tag contains `do not remove` (case-insensitive).
- It carries a foreign lifecycle marker (`aws:dlm:*` or `aws:backup:*` tag).
- It has no `VolumeId` in the API response (the keep-newest grouping would be
  undefined, so it fails closed).

EC2 reaper. An instance or its cluster is spared when any of these hold:

- The instance has a valid `iit-billing-tag`: a non-numeric category value, or a
  numeric unix-epoch timestamp that is still in the future. Exception: category
  tags matching `molecule_billing_pattern` (default `.*_package_testing$`) are
  age-bounded, not exempt; an aborted molecule build skips `molecule destroy`
  and leaks the instance, so matches older than `molecule_max_age_hours`
  (default 7) are reaped. Both knobs live in the `ec2_cleanup` block of
  `locals.tf`.
- It is an EKS instance whose `eksctl-<cluster>-cluster` CloudFormation stack
  carries a valid billing tag (same category-or-future-epoch rule).
- Its cluster name matches the EKS skip regex `eks_skip_pattern` (default
  `pe-.*`), set in the `ec2_cleanup` block of `locals.tf`.

An EKS instance with no valid billing tag (instance or stack) gets its
`eksctl-<cluster>-cluster` stack marked for deletion. The reaper's own backing
resources carry `PerconaKeep=True` from the root `local.tags`, so it never reaps
itself.

## Pause and rollback

To pause a reaper, reverse the arming change: set `dry_run = "true"` in its
`locals.tf` block, then `just tf-plan` and `just tf-apply`. It returns to
log-only and deletes nothing.

For a hard stop (no invocations at all), set the module's `schedule_enabled =
false` on the relevant instantiation in `volume-cleanup.tf`, `ec2-cleanup.tf`
or `snapshot-cleanup.tf`, then plan and apply. That disables the EventBridge rule
so the function is never triggered. Reserved concurrency `1` already guarantees a
stuck run cannot overlap the next schedule.

## Other reapers in the account (not ours)

This repo's two reapers are not the only cleanup automation in the
account. The cloud team runs an hourly `deleteOrphaned*` Lambda suite
(eu-west-3) that terminates running instances tagged `team=cloud`
without a `delete-cluster-after-hours` TTL tag, assuming they are
orphaned OpenShift test clusters; sibling functions sweep VPC contents
and CloudFormation stacks the same way. It terminated the first
Terraform cloud.cd master within an hour of its cutover, which is why
that master is tagged `team=cloud-cd`. Before giving any new resource a
`team` value, check what automation matches it. PMM also keeps two
legacy volume-cleanup functions on the shared `EventVolumeCleanup` rule
(next section).

## Shared-rule caution: do not delete `EventVolumeCleanup`

The legacy volume-cleanup stack's EventBridge rule `EventVolumeCleanup`
(us-east-1) was retained at decommission and is NOT managed by this repo. Two PMM
cleanup functions, `PMMAMICleanUp` and `PMMVolumeCleanUp`, are targets on it.
Deleting the rule would silently break those PMM reapers.

Never delete `EventVolumeCleanup` without first listing its targets and
confirming nothing else depends on it:

```sh
aws events list-targets-by-rule --rule EventVolumeCleanup --region us-east-1
```

## Tests

The reaper handlers and their Terraform wiring are covered by 59 tests (moto
behavior plus the IAM-policy and Terraform-contract checks). Run them locally:

```sh
just lambda-test    # also part of `just ci` / the cleanup-lambda tests CI job
```

The same suite runs in CI as the `cleanup-lambda tests` job, so a contract break
(for example arming `locals.tf` without updating
`test_dry_run_locked_to_committed_state`) fails the PR.

## Related

- [`terraform/modules/scheduled-lambda/README.md`](../../terraform/modules/scheduled-lambda/README.md)
  -- the generic cron-Lambda module the reapers instantiate.
- [`terraform/volume-cleanup.tf`](../../terraform/volume-cleanup.tf),
  [`terraform/ec2-cleanup.tf`](../../terraform/ec2-cleanup.tf),
  [`terraform/snapshot-cleanup.tf`](../../terraform/snapshot-cleanup.tf)
  -- per-reaper least-privilege policy and module instantiation.
- [`terraform/locals.tf`](../../terraform/locals.tf) -- the `Cleanup Lambda
  parameters` block (schedules, `dry_run`, age floors, EKS skip regex).
- [`orphaned-snapshot-cleanup.md`](orphaned-snapshot-cleanup.md) -- the manual
  sweep for CloudFormation orphan snapshots, which stay out of reaper scope.
