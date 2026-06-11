# 0034 — Split the OpenTofu state into `structural` + `ec2-masters` roots

**Status:** Accepted (2026-06-11). Execution pending, tracked in [`docs/runbooks/state-split-structural-ec2-masters.md`](../runbooks/state-split-structural-ec2-masters.md).
**Relates to:** [ADR 0024](0024-jenkins-fleet-ownership-boundary.md) (preserves its five-layer ownership model unchanged; this changes only the physical state topology under the single "AWS substrate = Terraform" layer).

## Context

ADR 0024 drew the substrate-vs-fleet ownership boundary but left the physical state topology as one OpenTofu root with one state file. That single state now holds the EKS hub and the whole Jenkins fleet together, and the coupling is felt two ways: a fleet change re-plans the hub and a hub change re-plans the fleet, and `tf-plan-masters` had to introduce a `-target` sweep as the daily review path so a master change could be planned without the whole hub. An exceptional flag promoted to routine use is the threshold-crossing tell.

The fleet has also grown past the point where one state is comfortable. As of 2026-06-11 it is:

- **8 Terraform EC2 masters**, each a full `module.<inst>` + `module.<inst>_arm_fleet` plus its EKS peering and routes: `cloud`, `pmm`, `ps57`, `ps80`, `psmdb`, `pxb`, `pxc`, `rel`.
- **`ps3`**: the classic EC2 master was retired (it now runs in-cluster as `jenkins-ps3-k8s`). `master-ps3.tf` keeps only `module.ps3_arm_fleet` (`jenkins-arm-standalone`) plus the EKS↔ps3 peering and routes, so the in-cluster controller keeps its aarch64 worker fallback.
- **`pg`**: the lone remaining CloudFormation master (eu-central-1). `master-pg.tf` provisions only `module.pg_arm_fleet` (a Fleet-only worker ASG) in Terraform; the master itself stays CFN.

The four CFN masters that ADR-era notes still called "to migrate later" (`psmdb`, `rel`, `cloud`, `pmm`) have already cut over to Terraform into this single state. Only `pg` remains CFN.

## Decision

### 1. Two roots, one bucket

Split the single root into two OpenTofu roots sharing the existing state bucket:

- **`structural/`** (key `structural/terraform.tfstate`): the EKS hub and everything that bootstraps it (VPC, EKS, addons, Karpenter, ACM, LGTM, ECR, backup, pod-identity, ArgoCD, Authentik), the account-hygiene reapers, the repo-wide CI/OIDC roles, and the `kubernetes`/`helm`/`kubectl` providers.
- **`ec2-masters/`** (key `ec2-masters/terraform.tfstate`): every master module, every ARM fleet (including `ps3_arm_fleet` and `pg_arm_fleet`), the cross-region peering and routes, and per-master IAM (already inside the `jenkins-master` module). No `kubernetes`/`helm`/`kubectl` providers.

This severs the shared-state cascade along the boundary ADR 0024 already drew, gives each layer its own lock and lockfile, and replaces the `-target` workaround with a real full plan that has full drift detection.

### 2. Cross-stack contract: three outputs

`ec2-masters` reads exactly three hub facts from `structural` through one `data.terraform_remote_state.structural` block: `vpc_id`, `private_route_table_ids`, and `vpc_cidr_block`. `outputs.tf` exports only `vpc_id` today, so the other two are added first (runbook Phase 1) as a zero-infra change. These three become a stable contract: renaming one silently breaks every master plan with no cross-state type check, so they are covered in CI.

### 3. Mechanism: `terraform_remote_state`, not SSM, not tag lookups

The `for_each` over `private_route_table_ids` requires plan-time-known keys. A `terraform_remote_state` output is a concrete value already materialized in the structural state when `ec2-masters` plans, so the key set is known with no `-target`. An `aws_ssm_parameter` value is always marked sensitive, and `for_each` over a sensitive value is a hard error unless wrapped in `nonsensitive()` — an avoidable foot-gun. A tag lookup would be a live-discovery contract rather than an explicit one. The plan-time-known property is a gate, not a hope: it holds only while the hub private-route-table set is stable and `structural` has been applied first, so structural-applied-first is encoded as an ordering guard.

### 4. Migration by whole-module state moves, not `import`

Relocation is done with `tofu state mv` on pulled local state copies plus a declarative `removed { lifecycle { destroy = false } }` drop on the structural side, not `import` blocks. `import` adopts one resource at a time by real-world ID; importing a whole master module (~35 resources) would mean hand-collecting ~35 IDs per master. `tofu state mv` moves an entire module subtree atomically with no IDs. The safety net is the pre-flight state backup + S3 versioning, building `ec2-masters` by pushing to an empty new key, the declarative `removed{destroy=false}` forgets, and a dual clean-plan gate, one master at a time.

### 5. Carve scope follows the substrate-vs-fleet boundary

`structural` keeps the hub; `ec2-masters` takes all fleet substrate. The 8 full masters move with their 6 addresses each. `ps3` moves too (its arm-fleet standalone plus the EKS↔ps3 peering and routes, 5 addresses, no master module). `pg`'s arm fleet moves; `pg`'s CFN master lands directly in `ec2-masters` at its eventual CFN→TF cutover, so it never perturbs `structural`.

## Consequences

- Separate locks and lockfiles; the `-target` recipe is deleted in favour of a real `ec2-masters` plan.
- `terraform_remote_state` grants the `ec2-masters` operator read of the whole `structural` state snapshot. Acceptable here: one team, one bucket, shared credentials.
- `versions.tf` (engine/provider pins, `local.modules`/`local.charts`) is duplicated per root, a new drift failure mode; `scripts/check_versions.py` checks both.
- The three structural outputs are a stable cross-state contract covered in CI.
- structural-applied-first becomes a hard ordering dependency (the `for_each` gate); a mid-change structural state (single-NAT flip, AZ change, VPC module major bump in flight) makes the keys unknown and errors `Invalid for_each argument`.
- Two states write into the same hub route tables (structural owns local + NAT routes, `ec2-masters` owns peering routes); they do not collide by route address today, noted in both roots.

## Alternatives considered

- **Per-master states (10 backends).** Rejected: 10× the cross-state surgery, no fleet-wide plan, and the marginal isolation does not pay for a 10-master, 5-region, PR-reviewed fleet.
- **Stay single-state.** Rejected: buys nothing at the 10-master end-state and institutionalizes the exceptional `-target` flag as the daily review path.
- **A third `shared-iam` state** (would make `structural` alias-free by moving the 3 CI/OIDC roots out). Deferred: do not split a third state without cause.

## Note on ADR 0024

ADR 0024's Context lists `$Latest` launch-template drift as a "Terraform pain." That was a CloudFormation behaviour; only the shared-state cascade is a genuine state-coupling data point, and it is the one this split addresses. The five-layer ownership model itself is preserved verbatim.
