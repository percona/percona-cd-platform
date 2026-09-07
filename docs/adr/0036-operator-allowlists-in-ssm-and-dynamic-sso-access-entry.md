<!-- Copyright (C) 2026 Percona LLC -->
# 0036: Operator allowlists in SSM and a dynamic SSO access entry

**Status:** Accepted (2026-06-12)
**Related:** [ADR 0032](0032-ec2-instance-connect-over-ssm-for-operator-ssh.md) (the break-glass :22 allowlist that becomes this mechanism's second tenant), [docs/eks-hardening.md](../eks-hardening.md) items #1 and #2.

## Context

The repo is public. Two security-critical EKS inputs (the API endpoint allowlist and the cluster-admin access entries) therefore lived only in each operator's gitignored `terraform/local.auto.tfvars`. That kept literals out of git but made who-can-kubectl invisible to PR review, let operator copies diverge, and meant a checkout without the local file would plan the removal of the only human admin entry (`authentication_mode = "API"` with creator-admin disabled).

## Decision

- The baseline cluster-admin access entry is committed code: `data "aws_iam_roles"` resolves the IAM Identity Center AdministratorAccess role at plan time (`name_regex` plus `path_prefix`, exactly one match enforced by postcondition). No account ID or role-suffix literal is committed, and permission-set re-provisioning self-heals on the next plan. `var.access_entries` stays as an override channel merged on top.
- CIDR allowlists move to SSM StringList parameters under `/<cluster>/allowlist/` (today: `eks-api`). Terraform only data-reads them. The parameters are deliberately NOT `aws_ssm_parameter` resources, because managing them would put the values back into the public repo. Writes go through `just allowlist-set` and are CloudTrail-audited.
- Fail-closed validation lives in data-source postconditions (plan-fatal), not `check` blocks (warn-only in OpenTofu 1.11): the parameter must exist, parse to at least one valid CIDR, and never contain `0.0.0.0/0`.

## Consequences

- The reviewable surface (who is cluster admin, where the allowlist lives, its guards) is committed. Only the IP values stay out of the repo, in one shared, audited location instead of N operator laptops.
- Resolved values still transit Terraform state (private S3 bucket), which is accepted.
- A fresh fork or DR rebuild must seed its parameters before the first plan (`ParameterNotFound` otherwise). See [docs/runbooks/eks-api-access.md](../runbooks/eks-api-access.md). CI is unaffected: `tofu validate` reads no data sources.
- The break-glass :22 allowlist (ADR 0032) joined as `/<cluster>/allowlist/master-ssh`: the module default is now empty (no committed IPs) and every `master-*.tf` passes the SSM-resolved list. The seed value is the union of the previously committed 6-CIDR baseline and the psmdb/pmm extras, so nobody loses access at cutover.
