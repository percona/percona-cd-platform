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
| `ssh` / `scp` / `rsync` over SSM | File transfer, or existing scripts that call `ssh`/`scp` | AWS creds + session-manager-plugin + your key on the master |
| `ssh <public IP>` | SSM unavailable, last resort | Your key on the master + your IP on the :22 allow-list |
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

ps3 has no EC2 master; `just ssh ps3` and `just ssm-run ps3 '<cmd>'` exec into
the controller pod's `jenkins` container directly (run `just kubeconfig` once
first). The manual equivalent:

```sh
kubectl --context percona-ci-platform -n jenkins-ps3-k8s exec -it jenkins-ps3-k8s-0 -c jenkins -- bash
```

## Copying files (scp / rsync over SSM)

The SSM tunnel carries plain SSH, so `scp` and `rsync` work with no inbound :22
and no public-IP lookup. Add one block per master to `~/.ssh/config`. It opens
an `AWS-StartSSHSession` to the master and resolves the instance by its billing
tag at connect time, so it survives instance rotation (the ID is never pinned).
After this, ordinary `ssh`/`scp`/`rsync` against the hostname just work,
including existing scripts that already call `ssh user@<inst>.cd.percona.com`.

```sshconfig
# ~/.ssh/config  (psmdb; swap region + tag for another master)
Host psmdb.cd.percona.com
  User ec2-user
  IdentityFile ~/.ssh/percona-jenkins.pem
  StrictHostKeyChecking no
  UserKnownHostsFile /dev/null
  ProxyCommand sh -c 'ID=$(aws ec2 describe-instances --region us-west-2 \
    --filters "Name=tag:iit-billing-tag,Values=jenkins-psmdb" "Name=instance-state-name,Values=running" \
    --query "Reservations[].Instances[].InstanceId" --output text); \
    exec aws ssm start-session --target "$ID" --region us-west-2 \
      --document-name AWS-StartSSHSession --parameters portNumber=%p'
```

Prereqs: `export AWS_PROFILE=percona-dev-admin` (SSO active),
session-manager-plugin installed, and your key in the master's
`ssh_key_engineers` (or the shared `percona-jenkins.pem`). Then:

```sh
# interactive shell
ssh psmdb.cd.percona.com

# copy a file UP (local -> master)
scp ./build.tar.gz psmdb.cd.percona.com:/tmp/

# copy a file DOWN (master -> local)
scp psmdb.cd.percona.com:/var/log/jenkins/jenkins.log ./

# copy a directory both ways
scp -r ./conf psmdb.cd.percona.com:/tmp/conf
scp -r psmdb.cd.percona.com:/tmp/conf ./conf-back

# rsync (incremental, resumable)
rsync -avz -e ssh ./artifacts/ psmdb.cd.percona.com:/tmp/artifacts/
rsync -avz -e ssh psmdb.cd.percona.com:/tmp/artifacts/ ./artifacts/
```

Region + tag per master: pmm `us-east-2`/`jenkins-pmm-amzn2`, psmdb/ps80/pxb
`us-west-2`, pxc `us-west-1`, ps57/pg `eu-central-1`, rel/cloud `eu-west-1`
(tag `jenkins-<inst>`).

## Background

Why the hostnames moved to the ALB, and how traffic reaches a master over
cross-region peering: [`../architecture.md`](../architecture.md) and
[`jenkins-ssl-cutover.md`](jenkins-ssl-cutover.md). The masters run with
`AmazonSSMManagedInstanceCore`, so SSM works without any inbound rule.
