<!-- Copyright (C) 2026 Percona LLC -->
# 0036: Operator allowlists in SSM and a dynamic SSO access entry

**Status:** Accepted (2026-06-12)
**Related:** [ADR 0032](0032-ec2-instance-connect-over-ssm-for-operator-ssh.md) (the break-glass :22 allowlist that becomes this mechanism's second tenant), [docs/eks-hardening.md](../eks-hardening.md) items #1 and #2.

## Context

The repo is public. Two security-critical EKS inputs (the API endpoint allowlist and the cluster-admin access entries) therefore lived only in each operator's gitignored `terraform/local.auto.tfvars`. That kept literals out of git but made who-can-kubectl invisible to PR review, let operator copies diverge, and meant a checkout without the local file would plan the removal of the only human admin entry (`authentication_mode = "API"` with creator-admin disabled).

## Decision

- The baseline cluster-admin access entry is committed code: `data "aws_iam_roles"` resolves the IAM Identity Center AdministratorAccess role at plan time (`name_regex` plus `path_prefix`, exactly one match enforced by postcondition). No account ID or role-suffix literal is committed, and permission-set re-provisioning self-heals on the next plan. `var.access_entries` stays as an override channel merged on top.
- CIDR allowlists move to SSM StringList parameters under `/<cluster>/allowlist/` (today: `eks-api`). Terraform only data-reads them; the parameters are deliberately NOT `aws_ssm_parameter` resources, because managing them would put the values back into the public repo. Writes go through `just allowlist-set` and are CloudTrail-audited.
- Fail-closed validation lives in data-source postconditions (plan-fatal), not `check` blocks (warn-only in OpenTofu 1.11): the parameter must exist, parse to at least one valid CIDR, and never contain `0.0.0.0/0`.

## Consequences

- The reviewable surface (who is cluster admin, where the allowlist lives, its guards) is committed; only the IP values stay out of the repo, in one shared, audited location instead of N operator laptops.
- Resolved values still transit Terraform state (private S3 bucket), which is accepted.
- A fresh fork or DR rebuild must seed its parameters before the first plan (`ParameterNotFound` otherwise); see [docs/runbooks/operator-allowlists.md](../runbooks/operator-allowlists.md). CI is unaffected: `tofu validate` reads no data sources.
- The break-glass :22 allowlist (ADR 0032) joins as `/<cluster>/allowlist/master-ssh` once its CIDR owners are confirmed; until then it stays a module default plus per-master overrides.
