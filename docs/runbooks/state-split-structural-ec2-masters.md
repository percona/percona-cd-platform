# Runbook: split one root into `structural` + `ec2-masters`

**Status:** Planned. Pre-flight gates verified live 2026-06-07; scope refreshed 2026-06-11.
**Decision + rationale:** [ADR 0034](../adr/0034-opentofu-state-split-structural-ec2-masters.md). This runbook is the execution procedure only; the why, the alternatives, and the cross-stack mechanism choice live in the ADR.
**Ticket:** PS-TBD.

## What this does

Split the single OpenTofu state into two roots sharing the existing S3 bucket: `structural/` (the EKS hub and its bootstrap) and `ec2-masters/` (all masters, ARM fleets, peering/routes, per-master IAM, reading three hub facts via `data.terraform_remote_state.structural`). See ADR 0034 for the boundary and the mechanism. This document covers the dangerous part: how to carve live state without destroying a master.

## Scope (refreshed 2026-06-11)

The fleet moved since the original draft. Carve everything that is already Terraform now; only `pg`'s master is still a future CFN→TF migration.

- **8 full Terraform masters** (`module.<inst>` + `module.<inst>_arm_fleet` + peering + routes): `cloud`, `pmm`, `ps57`, `ps80`, `psmdb`, `pxb`, `pxc`, `rel`. 6 addresses each.
- **`ps3`**: the EC2 master was retired (now in-cluster `jenkins-ps3-k8s`). `master-ps3.tf` carries `module.ps3_arm_fleet` (`jenkins-arm-standalone`) + the EKS↔ps3 peering and routes. 5 addresses, **no master module**. It still carves into `ec2-masters` as fleet substrate.
- **`pg`**: the lone CFN master (eu-central-1). `master-pg.tf` carries only `module.pg_arm_fleet` (Fleet-only worker ASG) + two re-read data sources. The arm fleet carves into `ec2-masters`; the `pg` master itself migrates CFN→TF **directly into `ec2-masters`** later (Phase 8) and never perturbs `structural`.

The original "carve 5 now, grow the rest in place" framing is retired: the four masters it called CFN (`psmdb`, `rel`, `cloud`, `pmm`) already cut over to Terraform, into this single state.

## Verified live facts (re-run before executing)

- State bucket versioning: **Enabled** (the only recovery path for a botched state write; `just tf-state-versioning-check`).
- Hub VPC has **1 private route table** (single NAT gateway) as of 2026-06-07, so each `aws_route.eks_private_to_<inst>` is a **1-element `for_each`**. This is the load-bearing condition for the cross-stack `for_each` (ADR 0034 §3); re-verify it has not changed before executing.
- Module roots in state today: the 8 masters above (each `module.<inst>` + `module.<inst>_arm_fleet`), `module.ps3_arm_fleet`, `module.pg_arm_fleet`, and the 3 CI/OIDC roots (`gha_jenkins_image_push`, `gha_percona_server_ec2_fallback`, `ppg_ami_factory_oidc`).
- Total resources in the single state: ~528 at 2026-06-07, higher now after the `psmdb`/`rel`/`cloud`/`pmm` cutovers. Confirm the live count at execution (`tofu -chdir=terraform state list | wc -l`); it is the blast-radius figure.
- `outputs.tf` exports `vpc_id` but **not** `private_route_table_ids` and **not** `vpc_cidr_block`. Both must be added before the split (Phase 1). **Still true as of 2026-06-11.**

## Target layout

```
terraform/
  structural/        # key=structural/terraform.tfstate
    backend.tf providers.tf versions.tf variables.tf locals.tf data.tf outputs.tf
    vpc.tf eks.tf eks-addons.tf eks-oidc-headlamp.tf karpenter-prereqs.tf
    acm.tf lgtm-storage.tf ecr.tf backup.tf pod-identity.tf
    ec2-cleanup.tf volume-cleanup.tf
    argocd.tf authentik.tf origins.tf
    iam-jenkins-controller.tf iam-jenkins-endpoint-reconciler.tf
    iam-gha-jenkins-image-push.tf iam-gha-percona-server-ec2-fallback.tf iam-gha-ppg-ami-factory.tf
    # providers: default aws (us-east-1) + aws.eu-central-1 (iam-gha-ppg-ami-factory) + kubernetes/helm/kubectl
    # outputs add: private_route_table_ids, vpc_cidr_block (keep vpc_id)
  ec2-masters/       # key=ec2-masters/terraform.tfstate
    backend.tf providers.tf versions.tf variables.tf data.tf remote-state.tf
    amis.tf          # moved here: the 4 regional SSM AMI lookups are master-only
    master-cloud.tf master-pmm.tf master-ps57.tf master-ps80.tf
    master-psmdb.tf master-pxb.tf master-pxc.tf master-rel.tf   # 8 full masters
    master-ps3.tf    # arm-fleet standalone + EKS<->ps3 peering/routes (no master module)
    master-pg.tf     # pg_arm_fleet only; pg master joins here at its CFN->TF cutover (Phase 8)
    # providers: default aws (us-east-1) + all regional aliases; NO kubernetes/helm/kubectl
  modules/           # shared, unchanged
    jenkins-master/ jenkins-arm-fleet/ jenkins-arm-standalone/ github-oidc-role/ scheduled-lambda/
```

Placement notes (grep-verified 2026-06-11):
- `amis.tf` moves to `ec2-masters` (its `al2023_minimal_*` SSM params are consumed only by `master-*.tf`, on the regional aliases).
- `origins.tf` stays in `structural`: its `data.aws_instances.<host>_master` live-IP lookups are dormant (commented out), so it does not actively couple to masters. Do not resurrect them; ephemeral master IPs are owned by the in-cluster EndpointSlice reconciler.
- The 3 CI/OIDC files stay in `structural`. Because `iam-gha-ppg-ami-factory.tf` builds in eu-central-1, `structural` keeps **one** regional alias (`aws.eu-central-1`). A third `shared-iam/` state would make `structural` alias-free; deferred (ADR 0034, do not split without cause).
- `ec2-cleanup.tf` / `volume-cleanup.tf` (account-hygiene reapers, default us-east-1, no master coupling) stay in `structural` with their `lambdas/` tree and the `scheduled-lambda` module.
- `versions.tf` is duplicated into each root; `scripts/check_versions.py` must check both copies.

## Cross-stack contract

`ec2-masters` reads three hub facts from `structural` via one block:

```hcl
# ec2-masters/remote-state.tf
data "terraform_remote_state" "structural" {
  backend = "s3"
  config = {
    bucket = "terraform-state-storage-percona-ci-platform"
    key    = "structural/terraform.tfstate"
    region = "us-east-1"
  }
}
```

Per master, rewrite the three hub references:
- `aws_vpc_peering_connection.<inst>.vpc_id`: `module.vpc.vpc_id` → `data.terraform_remote_state.structural.outputs.vpc_id`.
- `aws_route.eks_private_to_<inst>.for_each`: `toset(module.vpc.private_route_table_ids)` → `toset(data.terraform_remote_state.structural.outputs.private_route_table_ids)`.
- `aws_route.<inst>_to_eks.destination_cidr_block` and each master's `extra_http_ingress` cidr: `module.vpc.vpc_cidr_block` → `data.terraform_remote_state.structural.outputs.vpc_cidr_block`.

The reverse route `aws_route.<inst>_to_eks.for_each = toset(module.<inst>.private_route_table_ids)` is intra-state in `ec2-masters` (the master module's own static single-element output), so the split does not touch it. See ADR 0034 §3 for why this is `terraform_remote_state` and not SSM or a tag lookup, and why structural-applied-first is a gate not a hope.

## Migration mechanic (the dangerous part)

Whole-module relocation is done with **`tofu state mv` on pulled local copies plus a declarative `removed` drop on the structural side**, not `import` blocks (ADR 0034 §4). `prevent_destroy` is **not** the safety net during the move: it protects only while the resource block is in configuration, and the carve removes those blocks from `structural`. The real net is (a) the Phase 0 state backup + S3 versioning, (b) building `ec2-masters` by pushing to an empty new key, (c) dropping from `structural` via `removed { lifecycle { destroy = false } }` blocks that plan as forget-not-destroy, and (d) a dual clean-plan gate.

Per-master addresses to move (data sources re-read in `ec2-masters`, no move):

```
# 8 full masters — cloud, pmm, ps57, ps80, psmdb, pxb, pxc, rel (6 each):
module.<inst>
module.<inst>_arm_fleet
aws_vpc_peering_connection.<inst>
aws_vpc_peering_connection_accepter.<inst>
aws_route.eks_private_to_<inst>
aws_route.<inst>_to_eks

# ps3 — no master module (5 addresses):
module.ps3_arm_fleet
aws_vpc_peering_connection.ps3
aws_vpc_peering_connection_accepter.ps3
aws_route.eks_private_to_ps3
aws_route.ps3_to_eks

# pg — fleet only (1 address; master stays CFN until Phase 8):
module.pg_arm_fleet
```

## Phased execution

Run mutating steps one per message, verify live state after each, never batch them (repo `validate-first` rule). Every `tofu` call goes through `just`; the only raw-tofu exceptions are the read-only `state pull` and the local-copy `state mv`, flagged explicitly here.

**Phase 0 — pre-flight (re-run before executing).**
`just tf-state-versioning-check` (Enabled) and `just tf-state-backup` (snapshot under `terraform/.state-backups/`).

**Phase 1 — add the two outputs (zero-infra PR, safe to land now).**
In the current single root, add `output "private_route_table_ids" { value = module.vpc.private_route_table_ids }` and `output "vpc_cidr_block" { value = module.vpc.vpc_cidr_block }`. `just tf-plan` must show only the two new outputs, no resource changes. Land this PR first, independently.

**Phase 2 — scaffold the two roots (no state change).**
Create `terraform/structural/` and `terraform/ec2-masters/` with their own `backend.tf` (same bucket; keys `structural/terraform.tfstate` and `ec2-masters/terraform.tfstate`), `providers.tf` (split per the layout), and a duplicated `versions.tf`. `git mv` the `.tf` files into the two dirs (no code edits yet beyond providers/backend/remote-state). Move `amis.tf` into `ec2-masters`. `tofu fmt -recursive` and offline `validate` per root.

**Phase 3 — make `structural` the existing state (re-key, zero resource moves).**
Re-key the current backend to `structural/terraform.tfstate` and `tofu -chdir=terraform/structural init -migrate-state` (copies the live state to the new key; the larger half moves zero resources). Gate: `structural` plan is clean except for the masters showing as "to remove from config" (do not apply yet; Phases 4/5 add the `removed` blocks first).

**Phase 4 + 5 — carve each master into `ec2-masters` (one at a time).**
For `<inst>` in `cloud, pmm, ps57, ps80, psmdb, pxb, pxc, rel, ps3` (use the ps3 address set above; pg's fleet carves the same way with its single address):
1. In `ec2-masters/master-<inst>.tf`, rewrite the hub references to `data.terraform_remote_state.structural.outputs.*` (Cross-stack contract). ps3 and pg use the references they actually have (ps3 has peering+routes; pg's fleet has none).
2. In `structural`, add `removed { from = ... lifecycle { destroy = false } }` for that master's addresses (verify OpenTofu supports a module-address `removed`; else fall back to generated per-resource `removed` blocks). This protects against an accidental destroy in the double-tracking window.
3. `tofu -chdir=terraform/structural state pull > /tmp/s.tfstate`; for each address `tofu state mv -state=/tmp/s.tfstate -state-out=/tmp/m-<inst>.tfstate <addr> <addr>`; `tofu -chdir=terraform/ec2-masters state push /tmp/m-<inst>.tfstate` (pushing to the empty/new key is low risk).
4. **Dual clean-plan GATE:** `ec2-masters` plan for this master MUST be a no-op (same addresses, only data-source-backed values changed and equal), AND `structural` plan MUST show only the `removed` forgets (zero destroy/create). A non-empty plan means a botched move: STOP and restore from the Phase 0 snapshot.
5. Apply `structural` so the `removed` blocks drop the master from the structural state (forget, no destroy). The master is now fully in `ec2-masters` and gone from `structural`.

**Phase 6 — justfile + CI.**
Replace `tf-plan`/`tf-apply` with per-root recipes (`tf-plan-structural`/`tf-apply-structural`, `tf-plan-masters`/`tf-apply-masters` where `tf-plan-masters` is now a REAL full plan of `ec2-masters`). DELETE the `-target` recipe. Add a structural-applied-first ordering guard to `tf-apply-masters` (the `for_each` gate). Update `scripts/check_versions.py` and `just ci` to loop both roots.

**Phase 7 — ADR (DONE).**
The decision is recorded in [ADR 0034](../adr/0034-opentofu-state-split-structural-ec2-masters.md), which preserves ADR 0024's five-layer ownership model verbatim and corrects 0024's Context line that miscredited `$Latest` launch-template drift as a Terraform pain. No further action here.

**Phase 8 — pg routes into `ec2-masters`.**
`pg` (the only remaining CFN master) migrates directly into `ec2-masters` via the existing CFN→TF playbook and `jenkins-master-migration-{preflight,validate}` gates. Never touch `structural`.

**Phase 9 — module normalization (independent of the split; ordinary edits).**
Done once a master solely owns its state and any CFN stack is gone: delete the dead `create_worker_user` toggle; retire `launch_template_name` overrides (the `*MasterTemplateTF` coexistence names) and let the module derive `<SHORT>MasterTemplate`; collapse `init_groovy_hooks`/`plugin_install_hook` onto the S3 `init_groovy_files` path; rename psmdb's `worker_role_legacy_naming` resource at its cutover; replace the fragile `az_index` ordinal with an explicit `availability_zone` per master. Keep the real knobs (`purchasing_option`, instance types, `ebs_type`/`ebs_size`, `cache_bucket_name`, `create_eip`, `extra_http_ingress`, `extra_master_*_policies`). `create_route53_record` stays false (external-dns owns DNS). The module's toggle count must only decrease after the 10th master lands.

## Risk register

- **Botched cross-state move.** A wrong address or missing `removed` block orphans or destroys a live master; the EBS data volume is irreplaceable. Mitigation: Phase 0 snapshot + versioning, declarative `removed{destroy=false}` on the structural side, the dual clean-plan gate, one master at a time.
- **`for_each` plan-time failure** if the hub private-route-table set becomes unknown mid-change. Mitigation: keep the set stable; the structural-applied-first ordering gate (Phase 6).
- **Two states writing the same hub route tables** (structural owns local+NAT routes, ec2-masters owns peering routes). No collision by route address today; a careless structural-side route change could. Note in both roots.
- **Output-contract drift.** Renaming/removing a structural output silently breaks every master plan, no cross-state type check. Mitigation: treat the three outputs as a stable contract; cover them in CI.
- **versions.tf drift** between roots. Mitigation: `check_versions.py` checks both.
- **Doubled recipe surface** (apply in the wrong root). Partially offset by deleting the `-target` recipe.

## Rollback

Before the Phase 5 apply of any master: discard the local `/tmp/m-<inst>.tfstate`, leave `structural` untouched (its `removed` blocks unapplied), and live state is unchanged. After a bad push: restore the affected key from the Phase 0 snapshot via `state push` (S3 versioning gives prior object versions as a second recovery path). No `tofu apply` mutates real AWS during the carve (only state moves), so rollback is a state-restore, not an infra rebuild.

## Open items to confirm at execution

- OpenTofu module-address `removed` block syntax (`removed { from = module.ps80 }`); else generate per-resource `removed` blocks.
- Confirm no `structural` resource reads `amis.tf`'s `al2023_minimal_*` params before moving `amis.tf` (grep on 2026-06-11 shows only master files consume them; re-verify at execution).
- Re-confirm the hub still has a single private route table (the `for_each` gate) and the live resource count.
- Decide whether to split the optional `shared-iam` state or keep the 3 CI/OIDC files in `structural` with the single `eu-central-1` alias (default).
