# EKS API access

Two gates. Both must pass before `kubectl` works.

| Gate | Checks | Lives in | Changed by |
|---|---|---|---|
| Network | Source IP against the endpoint CIDR allowlist | SSM `/percona-ci-platform/allowlist/eks-api` | `just allowlist-set` (CloudTrail-audited) |
| Identity | Caller's IAM role against the cluster access entries | `terraform/eks.tf` (committed `sso_admin` lookup, `var.access_entries` overrides) | PR |

`authentication_mode = "API"`: access entries are the only identity path, there is no aws-auth ConfigMap. Decision record: [ADR 0036](../adr/0036-operator-allowlists-in-ssm-and-dynamic-sso-access-entry.md).

## Identity gate

Committed code. `eks.tf` resolves the IAM Identity Center AdministratorAccess role at plan time and grants it `AmazonEKSClusterAdminPolicy` at cluster scope. Nothing to manage day to day. Extra principals go in `var.access_entries` (gitignored `terraform/local.auto.tfvars`), merged on top.

## Network gate

A StringList of CIDRs, data-read at plan time (`terraform/allowlists.tf`). The plan fails on an empty list, an unparseable CIDR, or `0.0.0.0/0`.

Read:

```bash
just allowlist-show
```

Amend (the value is the full list, not a delta, and a put changes nothing live until the apply):

```bash
just allowlist-set eks-api "198.51.100.5/32,203.0.113.0/24"
just tf-plan && just tf-apply
```

## Emergency access

- Network: add a CIDR via `var.api_public_access_cidrs` in `local.auto.tfvars`, or edit live with `aws eks update-cluster-config` (the next apply reconciles back to the parameter).
- Identity: grant out-of-band with `aws eks create-access-entry` (the next apply removes entries not in code or tfvars).

Both paths are AWS API calls, not `kubectl`. Losing cluster access never blocks recovery.

## Bootstrap (fresh fork or DR rebuild)

The parameter must exist before the first plan, or the data source fails with `ParameterNotFound`:

```bash
aws ssm put-parameter --region us-east-1 \
  --name /<cluster>/allowlist/eks-api --type StringList \
  --value "<operator-egress>/32" \
  --tags Key=iit-billing-tag,Value=<cluster> Key=repo,Value=github.com/Percona/percona-cd-platform
```

## Second tenant: master-ssh

`/percona-ci-platform/allowlist/master-ssh` is the [ADR 0032](../adr/0032-ec2-instance-connect-over-ssm-for-operator-ssh.md) break-glass :22 list for the EC2 Jenkins masters. That gate protects master SSH, not the EKS API. See `master-shell-access.md`.

Same mechanics as eks-api: amend with `just allowlist-set master-ssh "<full,list>"`, then `just tf-plan` and `just tf-apply`. The module default is empty, so a master with no parameter value gets NO :22 ingress, and the plan fails closed if the parameter is missing, empty, or contains `0.0.0.0/0`.

Bootstrap (must exist before the first plan that includes this change):

```bash
aws ssm put-parameter --region us-east-1 \
  --name /<cluster>/allowlist/master-ssh --type StringList \
  --value "<operator-egress-cidrs,comma-separated>" \
  --tags Key=iit-billing-tag,Value=<cluster> Key=repo,Value=github.com/Percona/percona-cd-platform
```

The cutover seed is the union of the previously committed module default and the psmdb/pmm per-master extras (8 CIDRs). Total reachability is preserved, and the two per-master extras gain :22 on every master where they previously reached one master each; the six baseline CIDRs are unchanged.
