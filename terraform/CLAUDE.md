# terraform/ — conventions

Scoped instructions for this directory. Enforced fail-closed by
`scripts/check_conventions.py` (runs in `just ci` via the lint recipe and in
the CI `tofu` job); the repo-root `CLAUDE.md` carries the platform-wide rules.

## File naming grammar

1. **One AWS concern per file, named for the concern**: `vpc.tf`, `acm.tf`,
   `backup.tf`, `karpenter-prereqs.tf`, `pod-identity.tf`. Not the resource
   type (`s3-lgtm.tf`), not the provider (`aws-vpc.tf`).
2. **Repeated concerns form a prefix family**: `eks-*` (cluster lifecycle),
   `iam-*` (standalone IAM), `master-*` (one per Jenkins instance). Within
   `iam-*`: `iam-jenkins-<role>.tf` for Jenkins pod/role policies,
   `iam-gha-<product>-<purpose>.tf` for GitHub-OIDC roles.
3. **Wiring files keep conventional Terraform names** (`backend.tf`,
   `providers.tf`, `versions.tf`, `variables.tf`, `locals.tf`, `data.tf`,
   `outputs.tf`). Every `variable` block lives in `variables.tf`, never in a
   concern file. Concern-local `locals` for derived values are fine.
4. **Account-hygiene scheduled jobs are `<workload>-cleanup.tf`**
   (`ec2-cleanup.tf`, `volume-cleanup.tf`). "Cleanup" is the code vocabulary
   (module addresses, locals keys, function names); "reaper" stays prose-only
   in docs.
5. **One Jenkins instance = one `master-<inst>.tf`**, regardless of internal
   shape (full TF master, fleet-only against a CFN master, or ps3's
   fleet-remnant). When a CFN master migrates, its fleet file grows the
   master module under the same name; pmm lands as `master-pmm.tf`.
   Before cutover, diff the LIVE worker/master roles
   (`aws iam list-role-policies` + `list-attached-role-policies` +
   `get-role-policy` per inline name) against the CFN template: live roles
   carry out-of-band grants the template never listed (pmm's loss broke
   Packer AMI builds at `ec2:DescribeRegions`), and the module recreates
   the declared shape only. Carry deltas via `worker_ami_builder`,
   `worker_ecr_read`, `extra_worker_managed_policies`,
   `extra_master_managed_policies`.
6. **Addresses, `modules/` dir names, and `resources/addons/` basenames are
   contract surfaces** — state keys, `source` paths, namespaces, and
   Pod-Identity bindings hang off them. Never rename cosmetically. File
   renames are free (state keys by address); address renames need
   `tofu state mv` and a runbook.

## Owner banner

Line 1 of every `terraform/*.tf` and `terraform/modules/*/main.tf`:

```hcl
# Owner: platform | mysql | xtrabackup | pxc | mongodb | postgresql | release | cloud | pmm
```

Each product team is served by its own Jenkins instance(s):

| Instance | Team (= banner value) |
|---|---|
| ps3, ps57, ps80 | mysql |
| pxb | xtrabackup |
| pxc | pxc |
| psmdb | mongodb |
| pg (+ the ppg AMI factory) | postgresql |
| rel | release |
| cloud | cloud |
| pmm | pmm |

Everything else (wiring, eks-*, cleanup, iam-jenkins-*, substrate) is
`platform`. `iam-gha-percona-server-ec2-fallback.tf` is `mysql`.

## Comment rules

- **No copyright headers.** Percona's own IaC repos carry none; copyright
  headers belong to product source (enforced there via `.licenserc.yaml`).
- **No `CLAUDE.md` references** in `.tf` text — gotcha numbers renumber.
  Cite an ADR (`docs/adr/NNNN`) or a docs anchor (`docs/eks-hardening.md`,
  `docs/runbooks/...`) instead.
- **No Jira ticket IDs in comments** (`PS-`, `PKG-`, `PXB-`, `PG-`). Git
  history and the PR link the ticket; comments state the rationale directly.
  Functional values (`tickets = "PS-..."` module arguments, IAM `description`
  strings surfaced in the AWS console) are exempt from the gate — changing
  live-attribute descriptions is a plan diff, handle deliberately.
- **Third person**, no colleague names, no first-person we/our/us.
- **Dates only when load-bearing** ("since 2026-05", an incident that explains
  a sizing choice); move narratives to ADRs/runbooks.
- Keep the house strength: a header block stating WHAT the file owns and WHY
  the non-obvious choices were made, with ADR/docs anchors.

## Tags

`var.tags` (provider `default_tags`) is the floor; see repo `CLAUDE.md`
gotcha 7 and `docs/runbooks/cleanup-reapers.md` for the mandatory
`iit-billing-tag` + `PerconaKeep` pair. Resources spawned at RUNTIME
(launch templates, fleets, EC2NodeClass, StorageClass `tagSpecification_*`)
do not inherit `default_tags` and must re-assert the pair. Per-master billing
uses `iit-billing-tag = jenkins-<inst>` (master) and `jenkins-<inst>-worker`
(workers).

**`team` attribution tag**: every resource carries `team=<value from the
Owner set>` — `platform` via the `var.tags` default, overridden per master
through the jenkins modules' `team` variable (substrate + runtime
instances/volumes). Exists because `iit-billing-tag` alone can no longer
group a team's spend once a master moves in-cluster (shared StorageClass
bills to the platform). In the modules, module-set identity keys
(`iit-billing-tag`, `team`, `PerconaKeep`) merge LAST so a caller-supplied
map can never clobber them.
