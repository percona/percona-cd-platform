<!-- Copyright (C) 2026 Percona LLC -->
# 0037: EC2 master EBS snapshot RPO and snapshot-vehicle reconciliation

**Status:** Accepted (2026-06-18)
**Related:** [ADR 0026](0026-canary-dr-operating-model.md) (closes its flagged "24h EBS-RPO floor as an unmade decision"), [ADR 0028](0028-jenkins-dynamic-config-data-lifecycle.md) (the `workload=jenkins` vault-leak constraint, and the in-cluster snapscheduler vehicle this reconciles with), [ADR 0024](0024-jenkins-fleet-ownership-boundary.md) (ownership boundary: out-of-band resources have no owner), [docs/eks-hardening.md](../eks-hardening.md) item #14.

> **Implementation status: APPLIED (2026-06-18).** The Terraform DLM module and `ebs-snapshots.tf` are applied to AWS: one ENABLED policy per master region on the new role, and all nine master data volumes carry `snapshot-policy=jenkins-master` (pg tagged in place via `aws_ec2_tag`). Coverage is verified live. Update (2026-07-07): pg migrated to Terraform (PKG-1341); its data volume now carries the tag via `modules/jenkins-master` like the rest, retiring the `aws_ec2_tag` stopgap. STILL PENDING one daily cycle: the first daily snapshot firing per master volume, the cross-region copy landing in us-east-1, and the retirement of the 12 legacy console DLM policies (which includes the fb and ps3 orphans).

## Context

The durability of every EC2 Jenkins master was set by 12 hand-made AWS console DLM policies, created in 2023 on the console default role, present in no IaC. They have three problems:

1. **Inconsistent, mostly weak RPO.** Eight masters were on a weekly schedule (Sat 04:00 UTC, retain 4), so worst-case data loss was about seven days. Only pmm ran daily. The rest of the fleet already targets a 24h RPO (`backup.tf` snapshots cluster EBS daily with 14-day retention, and snapscheduler snapshots the in-cluster controller home daily/14), so the EC2 masters were the laggard.
2. **Single-region.** No policy had a cross-region copy, so a region loss destroyed the live volume and every snapshot of it together.
3. **Unowned.** Console resources drift to whoever last touched them, the exact failure [ADR 0024](0024-jenkins-fleet-ownership-boundary.md) warns against. Two policies (fb, ps3) were still enabled after their masters were decommissioned.

[ADR 0026](0026-canary-dr-operating-model.md) flagged the RPO floor as a decision to make explicit rather than inherit. This ADR closes it.

## Decision

### 1. RPO floor: daily, retain 14, cross-region copy

EC2 master DATA volumes are snapshotted **daily, 14 retained**, with a **cross-region copy to the cluster region (us-east-1)**. The explicit RPO floor is **24h**, single-region-loss tolerant. This matches the cluster-EBS and in-cluster cadences, so all three substrates converge on one number.

### 2. Vehicle: Terraform-managed DLM for the EC2 masters

The policies are recreated as a reusable per-region DLM module (`terraform/modules/dlm-snapshot/`, instantiated once per master region from `terraform/ebs-snapshots.tf`). DLM is chosen over AWS Backup for this population because it matches the legacy mechanism one-for-one (lowest-risk cutover), costs only snapshot storage, and stays clear of the `workload=jenkins` selection key. The three snapshot vehicles are reconciled, not unified:

- **DLM** for the EC2 masters (this ADR).
- **snapscheduler** for the in-cluster ps3-k8s controller home ([ADR 0028](0028-jenkins-dynamic-config-data-lifecycle.md)).
- **AWS Backup** for cluster stateful EBS (`backup.tf`).

### 3. Selection tag: `snapshot-policy=jenkins-master`, not `workload=jenkins`

A new uniform tag `snapshot-policy=jenkins-master` is set on every master data volume (and its launch-template volume tag block) in `modules/jenkins-master`, so one STRINGEQUALS selection covers a region. It deliberately avoids `workload=jenkins`, which [ADR 0028](0028-jenkins-dynamic-config-data-lifecycle.md) shows pulls a volume into the AWS Backup 14-day vault and leaks credentials. pg is CFN-managed, so its live volume is tagged in place (`aws_ec2_tag`) until pg migrates to Terraform.

### 4. Scope: data volumes only

Only the `JENKINS_HOME` data volumes are covered. Root/OS volumes are rebuildable from the AMI, user-data, and init.groovy.d, so snapshotting them adds storage cost with no recovery value.

## Consequences

- (+) RPO is now an explicit, uniform, IaC-audited 24h across all three substrates, and region loss is covered by the cross-region copy.
- (+) The unowned console policies (including the fb and ps3 orphans) are retired in favor of reviewable Terraform.
- (−) A master loss between daily snapshots can lose up to 24h of build history. Accepted for an internal CI fleet.
- (−) The cross-region copies add snapshot storage cost in the cluster region.
- (−) pg stays partially out-of-band (one `aws_ec2_tag`) until its CFN to Terraform migration.

## Acceptance criteria

- (met) Every master data volume (including pg) is matched by exactly one Terraform-managed DLM policy.
- (pending) A daily snapshot is observed per master data volume, and a cross-region copy lands in us-east-1 within one cycle.
- (pending) Zero policies remain on the console default DLM role, and the fb and ps3 orphans are gone.
- The existing snapshot history is preserved through the cutover.
