# Operating the cleanup reapers (volume + EC2)

Two scheduled AWS Lambda reapers keep the `percona-dev-admin` account tidy by
deleting resources that lack the mandatory cleanup tags. This runbook is how an
operator deploys them, reads their dry-run decisions, arms them for live
deletion, protects resources from them, and pauses or rolls them back.

Both reapers ship `DRY_RUN="true"`: they log every WOULD-delete decision but
delete nothing until deliberately armed. Arming is a reviewed code change, never
a one-off CLI flag.

## What runs

| Reaper | Function | Schedule | Sweeps | Targets |
|--------|----------|----------|--------|---------|
| Volume | `percona-ci-platform-volume-cleanup` | `rate(1 day)` | A fixed 8-region list in code | `available` (unattached) EBS volumes missing the cleanup opt-outs |
| EC2 | `percona-ci-platform-ec2-cleanup` | `rate(5 minutes)` | All regions via `describe_regions()` | Untagged EC2 instances; orphan `eksctl-<cluster>-cluster` stacks |

Both deploy as a single function in `us-east-1` (the default provider region);
the handlers iterate regions in code, so neither IAM policy carries an
`aws:RequestedRegion` condition. Each function logs to
`/aws/lambda/<function-name>`. Reserved concurrency is `1`, so a slow run can
never overlap itself.

The wiring (Lambda, EventBridge rule, log group, execution role) lives in the
`scheduled-lambda` module; see
[`terraform/modules/scheduled-lambda/README.md`](../../terraform/modules/scheduled-lambda/README.md).
The per-reaper policy and instantiation live in
[`terraform/volume-cleanup.tf`](../../terraform/volume-cleanup.tf)
and [`terraform/ec2-cleanup.tf`](../../terraform/ec2-cleanup.tf).
All tunables (schedules, `dry_run`, EKS skip regex, volume age floor) live in the
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

Both reapers ship `DRY_RUN="true"`, so they log WOULD-delete decisions and delete
nothing. Tail each log group and confirm the decisions look right before arming.

```sh
just lambda-logs ec2-cleanup        # wraps: aws logs tail /aws/lambda/percona-ci-platform-ec2-cleanup
just lambda-logs volume-cleanup     # wraps: aws logs tail /aws/lambda/percona-ci-platform-volume-cleanup
```

The EC2 reaper runs every 5 minutes, so a window appears quickly. The volume
reaper runs once a day; invoke it by hand to see a cycle now.

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

Let both bake in dry-run long enough to trust the decisions (one full daily cycle
for volume, several 5-minute cycles for EC2) before arming.

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

A contract test gates this on purpose. `test_dry_run_ships_true` in
[`terraform/lambdas/tests/test_terraform_contract.py`](../../terraform/lambdas/tests/test_terraform_contract.py)
asserts that the committed `dry_run` for both the `volume_cleanup` and
`ec2_cleanup` blocks is exactly `"true"`. The committed default must be the safe
one; arming is meant to be a deliberate, reviewed change rather than a value that
can drift unnoticed.

Because of that test, arming is a single PR that does two things together, by
design:

1. Edit `dry_run = "false"` in the relevant `locals.tf` block.
2. Adjust or remove the `test_dry_run_ships_true` expectation for that block in
   the same PR, so the test and the live state agree.

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
false` on the relevant instantiation in `volume-cleanup.tf` or
`ec2-cleanup.tf`, then plan and apply. That disables the EventBridge rule
so the function is never triggered. Reserved concurrency `1` already guarantees a
stuck run cannot overlap the next schedule.

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

The reaper handlers and their Terraform wiring are covered by 41 tests (moto
behavior plus the IAM-policy and Terraform-contract checks). Run them locally:

```sh
just lambda-test    # also part of `just ci` / the cleanup-lambda tests CI job
```

The same suite runs in CI as the `cleanup-lambda tests` job, so a contract break
(for example arming `locals.tf` without updating `test_dry_run_ships_true`) fails
the PR.

## Related

- [`terraform/modules/scheduled-lambda/README.md`](../../terraform/modules/scheduled-lambda/README.md)
  -- the generic cron-Lambda module the reapers instantiate.
- [`terraform/volume-cleanup.tf`](../../terraform/volume-cleanup.tf),
  [`terraform/ec2-cleanup.tf`](../../terraform/ec2-cleanup.tf)
  -- per-reaper least-privilege policy and module instantiation.
- [`terraform/locals.tf`](../../terraform/locals.tf) -- the `Cleanup Lambda
  parameters` block (schedules, `dry_run`, age floor, EKS skip regex).
