# Cleaning up orphaned EBS snapshots from retired masters

Use this runbook when a region accumulates self-owned EBS snapshots named
`<inst> DATA, do not remove` whose source Jenkins master is long
decommissioned. These are CloudFormation orphans, not managed backups, and they
are deleted by hand. Managed master snapshots are governed by the per-region DLM
policies and are never touched here.

## The failure pattern

The per-master CloudFormation stacks set `DeletionPolicy: Snapshot` (and
`UpdateReplacePolicy: Snapshot`) on the `JDataVolume` EBS resource. CloudFormation
takes a full-size snapshot of the data volume whenever the stack is deleted or the
volume resource is replaced. The resulting snapshots:

- Are named `<inst> DATA, do not remove` with an empty description.
- Carry `aws:cloudformation:stack-id`, `aws:cloudformation:stack-name`, and
  `aws:cloudformation:logical-id` tags.
- Have no lifecycle and no owner. Nothing ages them out.
- Accumulate over the life of a master, and outlive it after decommission.

Most reference zero blocks (the volume was empty at snapshot time), so they cost
almost nothing, but they clutter the snapshot list and obscure the managed
backups. A few hold real data.

## Identify an orphan versus a managed snapshot

An orphan is a self-owned snapshot whose source master is decommissioned and
whose source volume no longer exists.

- **DLM-managed (keep):** the snapshot carries an `aws:dlm:lifecycle-policy-id`
  tag. It belongs to one of the per-region DLM policies from ADR 0037 and is
  aged out automatically. Never delete it by hand.
- **CFN orphan (candidate):** no `aws:dlm:lifecycle-policy-id` tag, plus the
  `aws:cloudformation:*` tags and the `<inst> DATA, do not remove` Name. Its
  source volume is gone and its source master is retired.

List self-owned snapshots in a region and check for the DLM tag:

```sh
AWS_PROFILE=percona-dev-admin aws ec2 describe-snapshots --region <region> \
  --owner-ids self \
  --query 'Snapshots[].{id:SnapshotId,vol:VolumeId,t:StartTime,
    name:Tags[?Key==`Name`].Value|[0],
    dlm:Tags[?Key==`aws:dlm:lifecycle-policy-id`].Value|[0],
    cfn:Tags[?Key==`aws:cloudformation:stack-name`].Value|[0]}' \
  --output table
```

A row with `dlm` populated is a managed backup. A row with `dlm` null, a
`cfn` stack name set, and a Name of a retired master is an orphan candidate.

Confirm the source volume is gone (a missing volume means nothing live depends on
the snapshot):

```sh
AWS_PROFILE=percona-dev-admin aws ec2 describe-volumes --region <region> \
  --volume-ids <source-volume-id>
# InvalidVolume.NotFound means the source volume is already gone.
```

## Confirm a snapshot is safe to delete

A snapshot that references zero data blocks holds nothing recoverable. Use the
EBS direct API to read the block count; size is `block count * block size`.

```sh
AWS_PROFILE=percona-dev-admin aws ebs list-snapshot-blocks --region <region> \
  --snapshot-id <snap> \
  --query '{blocks:length(Blocks),blockSize:BlockSize}'
# blocks: 0  -> empty snapshot, nothing to lose.
# blocks > 0 -> holds real data; size is blocks * blockSize bytes.
```

Most orphans return zero blocks. For any snapshot that holds real data, confirm
the master is genuinely retired and the data is not needed before deleting.

## Delete procedure

Work one region at a time. Match the exact retired-master Name, never a live
master, and never the `eu-west-1` ps3 snapshots (those are retired separately by
the DLM legacy-policy cutover, see ADR 0037 and the ps3 decommission note in
[`decommission-ps3-ec2-master.md`](decommission-ps3-ec2-master.md)).

1. Re-confirm the snapshot is an orphan (no DLM tag, retired-master Name, source
   volume gone) and that the block count check above passed.

2. Delete it:

   ```sh
   AWS_PROFILE=percona-dev-admin aws ec2 delete-snapshot --region <region> \
     --snapshot-id <snap>
   ```

3. Re-check that zero orphans remain in the region. The describe-snapshots query
   above should now show only DLM-tagged managed snapshots and live-master
   snapshots:

   ```sh
   AWS_PROFILE=percona-dev-admin aws ec2 describe-snapshots --region <region> \
     --owner-ids self \
     --query 'Snapshots[?Tags[?Key==`Name` && contains(Value,`<inst>`)]].SnapshotId'
   # Expect: []
   ```

## What this cleanup last cleared (2026-06-18)

33 stale snapshots on long-decommissioned masters were deleted across three
regions: ps (14), the original ps3 / ps2 / ps56 in us-west-1, pt in us-west-2,
and dev-pmm in us-east-2. All but the pt snapshot referenced zero blocks; pt held
about 17 GB of real data. Live masters were never touched. The `fb` and
`eu-west-1` ps3 snapshots were deliberately left in place; they are retired by the
DLM legacy-policy cutover, not by this manual sweep.

## Related

- [ADR 0037](../adr/0037-master-ebs-snapshot-rpo.md): the per-region DLM
  policies that now govern managed master snapshots (daily, 14 retained, 24h
  RPO, cross-region copy), and the retirement of the legacy console DLM policies.
- [`disaster-recovery.md`](disaster-recovery.md): restoring an EC2 master data
  volume from a managed DLM snapshot.
- [`cleanup-reapers.md`](cleanup-reapers.md): the EC2, volume and snapshot
  reapers. The snapshot reaper ages out only `managed-by=ebs-csi-driver`
  (snapscheduler) snapshots; the CloudFormation orphans this runbook covers
  stay manual.
- [`decommission-ps3-ec2-master.md`](decommission-ps3-ec2-master.md): the
  ps3 retirement whose `eu-west-1` snapshots are out of scope here.
