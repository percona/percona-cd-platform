# terraform/

The AWS substrate, applied only through the repo justfile (`just tf-plan` /
`just tf-apply`; never raw `tofu`). `AWS_PROFILE` must be exported.

## What lives here

| Concern | Files |
|---|---|
| EKS cluster, node groups, addons | `eks*.tf`, `karpenter*.tf` |
| One file per Jenkins master | `master-<inst>.tf` (module `jenkins-master` + its `_arm_fleet` sibling) |
| Reusable modules | [`modules/`](modules/) (jenkins-master, jenkins-arm-fleet, jenkins-arm-standalone, scheduled-lambda) |
| Account-hygiene reapers | `ec2-cleanup.tf`, `volume-cleanup.tf` ([runbook](../docs/runbooks/cleanup-reapers.md)) |
| Version pins | `versions.tf` (verify bumps with `scripts/check_versions.py`) |

## Jenkins masters

Nine EC2 masters are fully Terraform-managed here (ps57, ps80, pxb, pxc,
psmdb, rel, cloud, pmm, pg), each EKS-fronted for HTTPS; ps3 runs in-cluster
and keeps only its ARM fleet substrate here.

Shell access to the masters: [`docs/runbooks/master-shell-access.md`](../docs/runbooks/master-shell-access.md).

## Conventions (gated)

File-naming grammar, `# Owner:` banners, comment and tag rules:
[`CLAUDE.md`](CLAUDE.md), enforced fail-closed by
`scripts/check_conventions.py` inside `just ci`.

Two tags are load-bearing on every resource (`iit-billing-tag`,
`PerconaKeep`); without them the account reapers delete the resource. Details:
repo [`CLAUDE.md`](../CLAUDE.md) gotcha 7.
