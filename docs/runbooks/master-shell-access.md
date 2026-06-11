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
| `ssh` / `scp` / `rsync` over SSM | File transfer, or existing scripts that call `ssh`/`scp` | AWS creds + session-manager-plugin + a local SSH key (EC2 Instance Connect, no provisioning) |
| `ssh <public IP>` | SSM unavailable, last resort | Your key on the master + your IP on the :22 allow-list |
| `kubectl exec` | ps3 only (in-cluster controller) | Cluster access (`just kubeconfig`) |

## ps3 (in-cluster)

ps3 has no EC2 master; `just ssh ps3` and `just ssm-run ps3 '<cmd>'` exec into
the controller pod's `jenkins` container directly (run `just kubeconfig` once
first). The manual equivalent:

```sh
kubectl --context percona-ci-platform -n jenkins-ps3-k8s exec -it jenkins-ps3-k8s-0 -c jenkins -- bash
```

## Copying files (scp / rsync over SSM)

The SSM tunnel carries plain SSH, so `scp` and `rsync` work with no inbound :22
and no public-IP lookup. You add one `~/.ssh/config` block per master; after
that, ordinary `ssh`/`scp`/`rsync` against the hostname just work, including
existing scripts that already call `ssh user@<inst>.cd.percona.com`.

The block resolves the instance by its billing tag at connect time, so it
survives instance rotation (the ID is never pinned), and routes the connection
through an `AWS-StartSSHSession`.

### 1. Prerequisites

- `export AWS_PROFILE=percona-dev-admin` with an active SSO session.
- [session-manager-plugin](https://docs.aws.amazon.com/systems-manager/latest/userguide/session-manager-working-with-install-plugin.html)
  installed locally.
- A local SSH key (any key works via EC2 Instance Connect, no provisioning; see
  [Authentication](#3-authentication)).

### 2. ssh_config block

This block routes the connection through SSM and, by default, pushes a
short-lived key via EC2 Instance Connect at connect time, so **any local key
works with no provisioning**. It resolves the instance by billing tag, so it
survives instance rotation.

```sshconfig
# ~/.ssh/config  (psmdb; swap region + tag for another master)
Host psmdb.cd.percona.com
  User ec2-user
  IdentityFile ~/.ssh/id_ed25519
  IdentitiesOnly yes
  StrictHostKeyChecking no
  UserKnownHostsFile /dev/null
  ProxyCommand sh -c 'ID=$(aws ec2 describe-instances --region us-west-2 \
      --filters "Name=tag:iit-billing-tag,Values=jenkins-psmdb" "Name=instance-state-name,Values=running" \
      --query "Reservations[].Instances[].InstanceId" --output text); \
    aws ec2-instance-connect send-ssh-public-key --region us-west-2 --instance-id "$ID" \
      --instance-os-user ec2-user --ssh-public-key "file://$HOME/.ssh/id_ed25519.pub" >/dev/null; \
    exec aws ssm start-session --target "$ID" --region us-west-2 \
      --document-name AWS-StartSSHSession --parameters portNumber=%p'
```

Region and tag per master: pmm `us-east-2`/`jenkins-pmm-amzn2`; psmdb, ps80, pxb
`us-west-2`; pxc `us-west-1`; ps57, pg `eu-central-1`; rel, cloud `eu-west-1`
(tag `jenkins-<inst>` unless noted).

### 3. Authentication

SSM only carries the transport (it replaces reaching port 22); SSH still
authenticates. Two ways to satisfy it:

- **EC2 Instance Connect (the block above, recommended).** The
  `send-ssh-public-key` step pushes a 60-second ephemeral key to `ec2-user`, so
  no `authorized_keys` provisioning is needed and any local key works. It is
  IAM-gated (`ec2-instance-connect:SendSSHPublicKey`) and every push is logged in
  CloudTrail. Requires the `ec2-instance-connect` package, installed fleet-wide
  and baked into user-data so it survives rebuilds (ADR 0032).
- **Static key (drop the EIC line).** If your key's public half is already in the
  master's `ssh_key_engineers`, remove the `send-ssh-public-key` line and point
  `IdentityFile` at that key; the shared `percona-jenkins.pem` is the fallback.
  `ssh_key_engineers` is set per master in `terraform/master-<inst>.tf` (declared
  in `terraform/modules/jenkins-master/variables.tf`); user-data writes the keys
  into `authorized_keys` on the next boot. Note the key TYPE must match exactly.
  Check yours is present:

  ```sh
  just ssm-run psmdb 'grep -qF "$(cut -d" " -f2 ~/.ssh/id_rsa.pub)" /home/ec2-user/.ssh/authorized_keys && echo provisioned || echo MISSING'
  ```

> The pure-shell paths (`just ssh` / `just ssm` / `just ssm-run`) need NO key at
> all; they run as the SSM `ssm-user` (root via `sudo`). A key is only needed
> for `ssh`/`scp`/`rsync` file transfer.

### 4. Commands

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

## Direct SSH to the public IP (last resort)

Use this only when SSM is unavailable. The SSM paths above do not use inbound
port 22 at all (the on-box agent dials out, and ssh runs to `localhost:22`
inside the tunnel), so they bypass the security group. This fallback is the only
path that needs port 22 open from the internet.

Two separate things are at play here, do not conflate them:

- **The master's public IP** (where you connect TO) is dynamic. The masters
  carry no Elastic IPs, so each gets a subnet-auto-assigned public IPv4 that
  CHANGES on every instance rotation or stop/start. Never pin anything to it;
  discover the current one with `just ssh` (the PUBLIC-IP column). The exceptions
  are pxc (keeps an EIP for an inbound JNLP agent pinned to it) and pg (still
  CFN, the EIP terminates its TLS).
- **`ssh_allowed_cidrs`** is the list of SOURCE IPs (yours) allowed to reach
  port 22. It is the SG ingress allow-list, unrelated to the master's own
  dynamic IP. Default is the 6-CIDR fleet baseline; per-master deltas are set in
  that master's `terraform/master-<inst>.tf`.

Engineer keys come from `ssh_key_engineers` in the same file (the same keys as
the SSM file-transfer path). If your IP changed, add it to `ssh_allowed_cidrs`
via PR; do not hand-edit the security group, a manual rule is stripped by the
next `tofu apply`.

## Background

Why the hostnames moved to the ALB, and how traffic reaches a master over
cross-region peering: [`../architecture.md`](../architecture.md) and
[`jenkins-ssl-cutover.md`](jenkins-ssl-cutover.md). The masters run with
`AmazonSSMManagedInstanceCore`, so SSM works without any inbound rule.
