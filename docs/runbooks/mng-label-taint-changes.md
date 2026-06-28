# Changing EKS managed node group labels or taints

Use this runbook whenever you edit the `labels` or `taints` map on an MNG
in `terraform/eks.tf`. It exists because letting `tofu apply` do it
directly will trigger a rolling drain on single-replica stateful MNGs
(`prometheus_system`, `jenkins_system`) and fail after ~25 min when the
resident workload's PodDisruptionBudget blocks eviction.

## Why the naive path fails

The AWS provider translates any change to `labels` or `taints` on an
`aws_eks_node_group` resource into an EKS `update-nodegroup-version` call
-- a **rolling node replacement**, not an in-place config patch. EKS
attempts to drain the old node, hits the resident workload's PDB:

- `prometheus_system`: Bitnami Authentik PG (1 replica, default chart PDB
  blocks the eviction), Grafana (2 replicas but PDB `minAvailable: 1`
  rejects the second eviction if both are on the same node), Authentik
  server (`pdb.minAvailable: 0` so it's evictable -- but PG isn't).
- `jenkins_system`: the single Jenkins master pod with `Retain`-bound
  100 Gi PVC; default chart PDB blocks the eviction.

After 4 retries (~25 min), tofu errors with:

```
unexpected state 'Failed', wanted target 'Successful'. last error:
ip-10-220-XX-XX.ec2.internal: PodEvictionFailure: Reached max retries
while trying to evict pods from nodes in node group ...
```

Tofu rolls back the MNG to its prior state, but a few things remain
inconsistent on the K8s side and the launch template stays half-updated
until the next apply.

## The correct path: AWS CLI ConfigUpdate

EKS supports an in-place label+taint update via
`update-nodegroup-config`. This is **NOT** a node refresh -- no drain,
no eviction, the live nodes get the new labels/taints via the kubelet
config that the EKS control plane patches. Takes ~10 seconds.

### Step 1 -- edit `terraform/eks.tf`

Source of truth stays in code. Change the `labels` and/or `taints` blocks
as you would normally.

### Step 2 -- apply the change via AWS CLI

For each MNG you touched, run:

```sh
# Substitute <nodegroup-name>, <new-labels>, <new-taints> as appropriate.
# `addOrUpdateLabels` and `removeLabels` together replace the full label
# map; same shape for taints. To find the current MNG name, use:
#   aws eks list-nodegroups --cluster-name percona-ci-platform --region us-east-1

AWS_PROFILE=percona-dev-admin aws eks update-nodegroup-config \
  --region us-east-1 \
  --cluster-name percona-ci-platform \
  --nodegroup-name <nodegroup-name> \
  --labels 'addOrUpdateLabels={key1=v1,key2=v2},removeLabels=[oldkey1,oldkey2]' \
  --taints 'addOrUpdateTaints=[{key=new,value=v,effect=NO_SCHEDULE}],removeTaints=[{key=old,value=v,effect=NO_SCHEDULE}]'
```

The response carries an `update.id`. Poll it until `status: Successful`:

```sh
AWS_PROFILE=percona-dev-admin aws eks describe-update \
  --region us-east-1 \
  --name percona-ci-platform \
  --nodegroup-name <nodegroup-name> \
  --update-id <update-id> \
  --query 'update.status' --output text
# Expect: InProgress -> Successful (~10s)
```

### Step 3 -- refresh tofu state

After the AWS CLI update, the live MNG matches the `eks.tf` config but
tofu state still reflects the pre-update values. Refresh:

```sh
# Raw-tofu exception: refresh-only + a targeted plan are intentionally NOT
# exposed as `just` recipes (the justfile entrypoint forbids -auto-approve and
# keeps -target plan-only; see CLAUDE.md). Run them directly here, with
# AWS_PROFILE exported (export AWS_PROFILE=percona-dev-admin).
tofu -chdir=terraform apply -refresh-only -auto-approve
tofu -chdir=terraform plan -target='module.eks.module.eks_managed_node_group["<mng-name>"]'
# Expect: "No changes. Your infrastructure matches the configuration."
# If labels/taints still show drift, the CLI update didn't match what's in
# eks.tf -- re-check both sides.
```

### Step 4 -- verify the live node

```sh
kubectl --context percona-ci-platform get node -l <new-label-selector> \
  -o jsonpath='{range .items[*]}{.metadata.name}{"\t"}{.spec.taints}{"\n"}{end}'
```

## When to let tofu roll the MNG anyway

The AWS-CLI path is for label/taint changes only. **AMI bumps** (when
the `release_version` field changes -- typically as a side-effect of the
SSM data source reading a newer AL2023 build) DO require a node rotation
and there is no way around the drain.

For those, plan for the eviction failure ahead of time:

1. Set `update_config.max_unavailable = 1` on the MNG (already chart
   default). For the single-replica stateful MNGs this is moot -- 1 of 1
   means full outage during drain.
2. Add `force_update_version = true` to the `aws_eks_node_group` block
   in `eks.tf` before applying. Forces eviction past PDB.
3. Or, scale up `max_size`/`desired_size` temporarily so EKS adds a new
   node first, lets the workload reschedule, then drains the old. Scale
   back down after.

## Arch flip (`ami_type`): a node-group REPLACEMENT, not a version update

Changing `ami_type` (e.g. `AL2023_x86_64_STANDARD` -> `AL2023_ARM_64_STANDARD`
for a Graviton migration) is different from the AMI-release bump above.
`ami_type` is ForceNew, so `tofu plan` shows the node group **must be replaced**
(`-/+`, "1 to add, 1 to destroy"), not an in-place `UpdateNodegroupVersion`.

This is SAFE: the terraform-aws-eks module sets `create_before_destroy = true`
plus `node_group_name_prefix` on `aws_eks_node_group.this`, so tofu creates the
**new** arm64 node group (a fresh generated name) and waits for it ACTIVE before
it destroys the old one. The bootstrap/stateless workloads reschedule onto the
new nodes during the old group's drain.

So the NO-GO test is NOT "stop on any replacement" (an arch flip always shows
replacement). The real distinction is the replacement ORDER. Read it from the
saved plan, do not eyeball `+/-` vs `-/+`:

```bash
# export AWS_PROFILE=percona-dev-admin
tofu -chdir=terraform plan -target='module.eks.module.eks_managed_node_group["<name>"]' -out=tfplan
tofu -chdir=terraform show -json tfplan \
  | jq -r '.resource_changes[] | select(.address|endswith("aws_eks_node_group.this[0]")) | .change.actions | @json'
# ["create","delete"]  -> create-before-destroy: SAFE, proceed
# ["delete","create"]  -> destroy-then-create:   STOP (would drop the node group before its replacement)
```

Caveat for the singleton stateful MNG (`jenkins_master`, `max_size=1`, RWO PVC):
create-before-destroy gives the controller a landing node, but the 100Gi PVC is
single-attach, so the controller cannot start on the new node until the old pod
releases the volume. Expect a brief controller restart (a transient
`Multi-Attach` during the hand-off is normal and self-resolves). Take a pre-roll
EBS snapshot of the PVC, and run it in a window. See
[ADR 0043](../adr/0043-platform-wide-arm64-migration.md).

## Past incidents this prevents

- 2026-05-11 16:43 UTC: legacy-label cleanup PR (PR #72). Tofu apply
  triggered a rolling drain on `prometheus_system` MNG (single-replica
  m6a.large in us-east-1a hosting Authentik PG + Grafana + Authentik
  server). Drain failed at 25 min because of the Authentik PG PDB.
  Authentik + Grafana spent ~25 min Pending until the live node was
  manually kubectl-tainted. Resolved post-incident with this runbook.

## Related

- [ADR 0008 -- Managed NGs for stateful/system workloads](../adr/0008-managed-ng-for-stateful-system-workloads.md)
- [ADR 0017 -- Cluster tier taxonomy and LGTM pinning](../adr/0017-cluster-tier-taxonomy-and-lgtm-pinning.md)
- AWS docs: [Update a managed node group](https://docs.aws.amazon.com/eks/latest/userguide/update-managed-node-group.html)
