<!-- Copyright (C) 2026 Percona LLC -->
# 0032 — EC2 Instance Connect over SSM for operator SSH

**Status:** Accepted (2026-06-11)
**Related:** [ADR 0019](0019-shared-alb-ssl-termination-for-jenkins-masters.md) (EKS fronting; named the EC2 Instance Connect Endpoint as preferred operator access, amended by this ADR), [ADR 0013](0013-push-from-masters-with-nginx-bearer.md) (every master is SSM-managed: `AmazonSSMManagedInstanceCore` is universal).

> **Update (2026-07-07):** pg migrated to Terraform (PKG-1341); its user-data codification now lives in the module.

## Context

After the CloudFormation-to-Terraform migration the master hostnames resolve to the shared `jenkins-cd` ALB (HTTPS only), so direct hostname SSH times out. Operators reach the masters through SSM Session Manager (the `AWS-StartSSHSession` tunnel) or, as a break-glass path, the dynamic public IP behind a `/32` allowlist. SSH key authentication was satisfied only by static keys baked into `ec2-user`'s `authorized_keys` from each master's `ssh_key_engineers` list.

Two facts made that the only key path, and a poor one:

1. ADR 0019 named the **EC2 Instance Connect Endpoint** (EICE) as the preferred operator-access mechanism, but no EICE resource was ever built.
2. The `al2023-ami-minimal` base ships **without** the `ec2-instance-connect` package, so ephemeral-key push did not work at all.

Static keys carry real cost: every engineer's key must be added to `ssh_key_engineers` via PR and applied on a rebuild before it works (and the key TYPE must match exactly), and static-key use leaves no API-level audit trail.

## Decision

- Install the `ec2-instance-connect` package in the `jenkins-master` user-data (next to `amazon-ssm-agent`), and install it live on the running fleet. Operators push a 60-second ephemeral key with `ec2-instance-connect:SendSSHPublicKey` and connect over the existing SSM `AWS-StartSSHSession` tunnel.
- Reach the masters over **SSM, not an EC2 Instance Connect Endpoint**. SSM is already the universal transport (every master carries `AmazonSSMManagedInstanceCore`, ADR 0013) and provides a superset of EICE: interactive shell, RunCommand, port forwarding, and session logging. An EICE would be a redundant second path to the same instances.
- Keep static `ssh_key_engineers` keys as a coexisting fallback: sshd tries the `AuthorizedKeysFile`, then the `AuthorizedKeysCommand` the package installs.
- Scope: the nine EC2 masters (`pmm`, `psmdb`, `ps80`, `pxb`, `pxc`, `ps57`, `pg`, `rel`, `cloud`). The in-cluster `ps3-k8s` controller is excluded; it has no EC2 instance and is reached via `kubectl exec`.

This supersedes ADR 0019's operator-access line (EICE preferred) with EIC over SSM.

## Consequences

- Operator `ssh`/`scp`/`rsync` needs no `authorized_keys` provisioning; any local key works, gated by IAM and audited in CloudTrail (`SendSSHPublicKey` events) which static keys never produced.
- **IAM is now an SSH authentication path.** Under the current admin-only access model (`percona-dev-admin` = AdministratorAccess) this does not widen effective access. If IAM is ever scoped to less-privileged roles, `ec2-instance-connect:SendSSHPublicKey` (and `ssm:StartSession`) must be controlled, optionally with the `ec2:osuser` condition key.
- No inbound `:22` is required for this path: the ephemeral key is validated on the host and the transport is the SSM tunnel to `localhost:22`. The `/32` allowlist (`ssh_allowed_cidrs`) remains only for the direct-public-IP break-glass path.
- `pg` is still CloudFormation, not the TF module. Its live install is captured here, but the user-data codification lands when `pg` migrates (or via its CFN template in the meantime).
- EICE remains the future option if the masters drop public IPs for a fully private posture, or if a non-SSM SSH path is ever needed. The package installed here is a prerequisite for EICE too, so this decision does not foreclose it.

## Alternatives considered

- **EC2 Instance Connect Endpoint (EICE)** — the 0019-stated preference. Deferred: SSM already provides the transport and a superset of capabilities; the masters still carry public IPs, so EICE's main benefit (reaching private, no-public-IP instances) is not yet needed; it would add a VPC resource, an endpoint security group, and IAM for a redundant path.
- **Static keys only (status quo)** — rejected as the primary path. Provisioning friction (a PR plus rebuild per engineer, exact key-type match) and no API audit trail. Kept as the fallback.
- **Bastion or SSH CA** — heavier to operate than reusing SSM plus EIC, with no advantage on an already-SSM-managed fleet.
