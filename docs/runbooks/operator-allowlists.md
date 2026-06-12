# Operator allowlists (SSM-backed)

The EKS public API allowlist lives in SSM Parameter Store, not in git (the
repo is public). Terraform data-reads it at plan time with fail-closed
postconditions. Decision record:
[ADR 0036](../adr/0036-operator-allowlists-in-ssm-and-dynamic-sso-access-entry.md).

| Parameter | Gates | Consumer |
|---|---|---|
| `/percona-ci-platform/allowlist/eks-api` | EKS public API endpoint | `terraform/allowlists.tf` -> `terraform/eks.tf` `endpoint_public_access_cidrs` |

Planned second tenant: `/percona-ci-platform/allowlist/master-ssh` (the
[ADR 0032](../adr/0032-ec2-instance-connect-over-ssm-for-operator-ssh.md)
break-glass :22 list) once its CIDR owners are confirmed.

## Read

```bash
just allowlist-show
```

## Amend

```bash
just allowlist-set eks-api "198.51.100.5/32,203.0.113.0/24"
just tf-plan
just tf-apply
```

The value is the FULL comma-separated list, not a delta. A put changes
nothing live until the plan and apply. The plan fails (postcondition) on an
empty list, an unparseable CIDR, or `0.0.0.0/0`.

## Emergency access

`var.api_public_access_cidrs` (gitignored `terraform/local.auto.tfvars`)
stays as an additive override while the parameter catches up. Out-of-band,
`aws eks update-cluster-config` edits the live allowlist directly; the next
apply reconciles it back to the parameter value. Losing kubectl access never
blocks recovery: both paths are IAM-plane.

## Bootstrap (fresh fork or DR rebuild)

The parameter must exist BEFORE the first plan, or the data source fails
with `ParameterNotFound`:

```bash
aws ssm put-parameter --region us-east-1 \
  --name /<cluster>/allowlist/eks-api --type StringList \
  --value "<operator-egress>/32" \
  --tags Key=iit-billing-tag,Value=<cluster> Key=repo,Value=github.com/Percona/percona-cd-platform
```
