# Shell access to the Jenkins masters

`ssh <inst>.cd.percona.com` no longer reaches an EKS-fronted master: the
hostname resolves to the shared `jenkins-masters` ALB, which terminates TLS and
serves only HTTPS. The symptom is `ssh: connect to host ... port 22: Operation
timed out`. This runbook is the maintainer path to a shell.

## TL;DR

```sh
export AWS_PROFILE=percona-dev-admin
just ssh                       # list the reachable masters (live from AWS)
just ssh pmm                   # interactive shell via SSM Session Manager
just ssm-run pmm 'df -h /mnt'  # one-shot root command, prints the output
```

`just ssh` discovers running masters by their `jenkins-*` billing tags across
the five home regions, so the list never goes stale. The interactive session
needs the [session-manager-plugin](https://docs.aws.amazon.com/systems-manager/latest/userguide/session-manager-working-with-install-plugin.html)
installed locally; `ssm-run` does not.

## Access paths, in preference order

| Path | When | Needs |
|---|---|---|
| `just ssh <inst>` / `just ssm <inst>` | Default. Interactive shell, no inbound :22 | AWS creds + session-manager-plugin |
| `just ssm-run <inst> '<cmd>'` | One-shot commands, scripting, no TTY | AWS creds only |
| `ssh <public IP>` | SSM unavailable, or file transfer with scp/rsync | Your key on the master + your IP on the :22 allow-list |
| `kubectl exec` | ps3 only (in-cluster controller) | Cluster access (`just kubeconfig`) |

## Direct SSH (the dynamic-IP path)

The masters carry no Elastic IPs: each gets a subnet-auto-assigned public
IPv4 that CHANGES on every instance rotation or stop/start, so never pin
anything to it; discover the current one with `just ssh` (the PUBLIC-IP
column). The exceptions are pxc (keeps an EIP for an inbound JNLP agent
pinned to it) and pg (still CFN, the EIP terminates its TLS). Port 22 is
open only to the allow-list in the `jenkins-master` module
(`ssh_allowed_cidrs`, default is the 6-CIDR fleet baseline; per-master deltas
are set in that master's `terraform/master-<inst>.tf`). Engineer keys are
provisioned from `ssh_key_engineers` in the same file.

If your IP changed, add it to `ssh_allowed_cidrs` via PR; do not hand-edit the
security group. A manual rule is stripped by the next `tofu apply`.

## ps3 (in-cluster)

ps3 has no EC2 master. The controller is the `jenkins-ps3-k8s-0` pod:

```sh
just kubeconfig
kubectl --context percona-ci-platform -n jenkins exec -it jenkins-ps3-k8s-0 -- bash
```

## Background

Why the hostnames moved to the ALB, and how traffic reaches a master over
cross-region peering: [`../architecture.md`](../architecture.md) and
[`jenkins-ssl-cutover.md`](jenkins-ssl-cutover.md). The masters run with
`AmazonSSMManagedInstanceCore`, so SSM works without any inbound rule.
