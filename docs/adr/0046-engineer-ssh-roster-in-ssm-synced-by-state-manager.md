<!-- Copyright (C) 2026 Percona LLC -->
# 0046 - Engineer SSH roster in SSM, synced to the masters by State Manager

**Status:** Accepted (2026-09-14)
**Amends:** [ADR 0032](0032-ec2-instance-connect-over-ssm-for-operator-ssh.md), whose static-key fallback was fed by a committed `ssh_key_engineers` list written once at boot. The fallback stays, its source and delivery change.
**Related:** [ADR 0036](0036-operator-allowlists-in-ssm-and-dynamic-sso-access-entry.md) (operator allowlists already live in SSM Parameter Store with the same write path), [ADR 0013](0013-push-from-masters-with-nginx-bearer.md) (every master carries `AmazonSSMManagedInstanceCore`), [ADR 0031](0031-jenkins-uptime-probes-and-alerting.md) (the Alertmanager has no receiver yet, which bounds the detection story below), [ADR 0044](0044-master-host-os-metrics.md) (the master-side Alloy unix exporter that scrapes the sync metrics).

## Context

The `ssh_key_engineers` list named the same eight engineers in every `master-*.tf` of a public repo. Three defects followed:

- The public repo doubled as a targeting list of who can SSH into CI.
- A roster edit was a ten-file change that drifted.
- Keys were written once at first boot, and `aws_instance.master` ignores `user_data` changes. Revoking an engineer meant a PR, an apply, and a rebuild of each master.

The fleet does not use these keys. Measured on 2026-09-14 on all nine production masters and the `ps80-upgraded` clone: `last` (wtmp, which persists for the whole uptime and on psmdb across a reboot) shows no interactive login other than reboots since each boot, 16 to 96 days, and the sshd journal (journald retains about two weeks on these hosts) shows no accepted public-key authentication of any kind in its window. CloudTrail shows `ssm:StartSession` in routine use and three `ec2-instance-connect:SendSSHPublicKey` calls in 90 days across all five regions. Operators use SSM Session Manager (`just ssh`), which never touches sshd. The static keys are a break-glass fallback for the day SSM and the EC2 API are both unavailable, and a fallback still needs a revocation path that works on a running master.

Counts used below: 8 slugs, 9 keys (one engineer publishes two), 10 EC2 instances (9 production masters plus the `ps80-upgraded` clone), all in five regions. ps3 runs in-cluster with no sshd and is out of scope.

## Decision

- The roster lives in one SSM Parameter Store parameter, `/<cluster>/access/master-ssh-engineers`, Standard tier, type `String`, value `{"schema":1,"engineers":["<slug>",...]}`, in the default provider region (`us-east-1`, the region the other two SSM tenants of ADR 0036 use). The masters read it cross-region. One fleet-wide list, not one per master: an offboarding is one write, and today's ten lists are identical anyway. Per-master scoping, if ever needed, belongs to the instance ARNs in the EC2 Instance Connect policy, not to a key file.
- Slugs, not keys. The public keys stay published by Percona IT at `percona.com/get/engineer/KEY/<slug>.pub`, so key rotation remains theirs. The nine current keys total 4977 bytes, over the 4 KB Standard limit, which also rules out storing them in the parameter.
- JSON rather than StringList because SSM rejects an empty value, and `"engineers": []` must be expressible: it is the revoke-all state.
- Terraform only names the parameter. It is not an `aws_ssm_parameter` resource and not a data source, so the roster enters neither the repo nor the state. Writes go through `just engineers-set`, every write is a CloudTrail `PutParameter`.
- Delivery is an `aws_ssm_association` per master, the pattern the module already uses for `init.groovy.d`. It runs once on creation, then every 30 minutes, and reaches the running fleet with no rebuild. `just engineers-set` also calls `StartAssociationsOnce`, so 30 minutes is the worst case, not the target. The association only exists for on-demand masters, and the module variable refuses a spot master rather than silently skipping it.
- The reconciler writes a dedicated file, `/etc/ssh/authorized_keys.d/ec2-user.engineers`, root-owned `0644` in a root-owned `0755` directory (sshd reads it as the login user), named by one `AuthorizedKeysFile` drop-in, `/etc/ssh/sshd_config.d/40-engineer-keys.conf`, `0600` like the packaged drop-ins, in a directory that keeps the packaged `0700`. The drop-in names the file as `%u.engineers`, so a user without a file of that name gains nothing, and `40-` sorts before the packaged and cloud-init drop-ins, so with sshd's first-value-wins rule it is the effective `AuthorizedKeysFile`. `~ec2-user/.ssh/authorized_keys` keeps only the EC2 key pair line, and the legacy engineer keys are pruned from it once, with a dated backup. Without the prune the new file would add keys and revoke nothing.
- Every fetched line is validated with `ssh-keygen -lf` before it is written. A 404 body is HTML and must never reach the file.
- Failure contract, in the order the run proceeds:
  - The roster cannot be read or does not parse: nothing on the host changes, the run is red.
  - The feed answers 404 for a slug: IT no longer publishes that engineer, the slug gets no key, is reported as missing, and the run stays green. A roster edit removes the slug. The feed answering 404 for every slug is treated as a feed fault: nothing changes, the run is red.
  - The feed fails for a slug in any other way (timeout, 5xx, a line `ssh-keygen` rejects): that slug keeps the keys it already had in the file, every other slug converges, and the run is red as degraded. A roster removal therefore lands even while another slug's feed is broken.
  - The roster file is published, then the drop-in is confirmed live (`sshd -t`, reload, `sshd -T` names the exact file), then the one-time prune runs. The prune waits while the run is degraded, and it refuses, naming the key, when the legacy file holds a key the roster file does not serve, so the one step that removes access never removes it from someone the roster does not know about.
  - A run that finds another run holding the lock exits green without touching files or metrics. The holder reports.
- Each run writes a textfile metric that the master-side Alloy unix exporter scrapes (ADR 0044). `engineer_keys_sync_roster_version` is the version last applied in full, never the version last read.
- No IAM change. `AmazonSSMManagedInstanceCore` already grants `ssm:GetParameter` on `*` to every master role. A grant scoped to the one parameter ARN is the tighter option and is not taken, because the roster is names, not secrets.

## Security and threat model

- Who can write the roster: whoever holds `ssm:PutParameter` on the parameter, in practice the AdministratorAccess role. That role already has SSM root on every master through Session Manager, so the write path adds no privilege. It adds durable persistence: a roster write re-applies itself on every master every 30 minutes and survives reboots. Incident response therefore targets the roster (or disables the association), not the host. A key removed from the roster file by hand is back within one tick. The legacy `authorized_keys` is pruned once, not on every run, so a key planted there after the prune is not reverted by this mechanism.
- What a roster write can and cannot do: it can only grant an engineer whose key IT publishes. To install a key of their own an attacker needs the percona.com feed as well.
- The feed: percona.com serves what Percona IT publishes from FreeIPA, regenerated every 12 hours, and the corporate bastions consume the same feed. The masters are a lower-value, fail-safe consumer: a feed outage keeps the last good file rather than locking anyone out. The fetch verifies TLS and bounds the wait, the body is validated key by key. The residual is that the feed is trusted on TLS plus key shape, not a signature, so a feed compromise can serve a valid key for a real slug. Accepted, as it is for the bastions.
- The script: the association pins `sha256(file("engineer-keys-sync.sh"))` from the reviewed source and the host verifies it before running, so the init-config bucket cannot change what runs. The master's policy on that bucket is read-only.
- The prune's evidence: the EC2 key pair line is read from instance metadata over IMDSv2 with a hop limit of 1, so a container on the master cannot forge it.
- The key pair: the `percona-jenkins` key pair is one key in all five regions, held outside this repo. It is the last fallback when the roster path and SSM are both down and it is unaffected by anything here.
- Detection is pull-only today. The alerts below fire in the Mimir Alertmanager, which routes to a receiver without integrations (ADR 0031), so a stalled sync is seen on the dashboards, in `just engineers-status`, and as a HIGH non-compliance in SSM, and it pages nobody. That is tolerable because a failed run never removes access. `EngineerKeysSyncMissing` only fires while the master still pushes host metrics, so a fully dark master reports here through the master-down alerts, not this one.

## Consequences

- Revocation lands fleet-wide within one tick and needs no rebuild, no PR, and no apply, including while another engineer's feed is broken.
- The roster is out of the repo and out of Terraform state. Git history, older launch-template versions, and instance user-data still hold the old lists. This decision reduces future exposure, it does not erase the past.
- The break-glass fallback depends on percona.com at sync time, as the boot script already did. During an outage stale keys persist until the next successful sync. If bounded revocation during an outage ever becomes a requirement, the alternative is a key snapshot in an Advanced-tier parameter. An IT-side key removal reaches the masters within 12 hours plus one tick.
- Association status is the health signal, and the textfile metrics carry it into Mimir. Nine alerts in the jenkins-uptime addon cover a failing run, two hours without success, a master that never reported, a degraded slug, a missing slug, version skew between masters, a pending legacy prune, and two informational ones for a roster change and a key set that changed without a roster change. `just ssm-run` masks the remote exit code, so verification reads the association execution status, never a shell exit code.
- A fresh fork or DR rebuild must seed the parameter before the associations run, otherwise the association reports failed convergence and the master carries no engineer keys. SSM, EC2 Instance Connect, and the EC2 key pair are unaffected either way. Until the observability script pin in the module's user-data is bumped past the textfile collector, a rebuilt master writes the metric file and nothing scrapes it, which `EngineerKeysSyncMissing` reports.
- This is an EC2-only mechanism on a fleet that is moving in-cluster. It retires with the last EC2 master and nothing here should be carried to the Kubernetes controllers, which have no sshd.
- The evidence above supports retiring the static keys entirely. That is a separate decision, gated on agreement from the people on the roster and a written break-glass for the case where SSM and the EC2 API are both unavailable. Under this design it costs one write: an empty roster. The usage measurement covers 90 days of the new mechanism in mid-December 2026, which is when that decision is due.

## Alternatives considered

- **Plan-time SSM data read (the ADR 0036 pattern).** Takes the roster out of the repo but puts it into state and user-data, and a change still reaches a running master only on rebuild. Rejected.
- **Gitignored tfvars.** Rejected for the reasons ADR 0036 gave: invisible to review, divergent operator copies, a checkout without the file plans a removal.
- **A boot-time reader plus a systemd timer.** Does not reach the running fleet without a rebuild. The State Manager association does, and it exposes execution results.
- **AWS Secrets Manager.** The roster is configuration, not a secret. Per-secret cost, rotation semantics nobody needs, and a VPC endpoint that does not exist yet. Rejected.
- **One parameter per master.** Recreates the duplication in SSM and turns offboarding into ten writes. Rejected.
- **FreeIPA enrolment (`ipa-client` and `sssd`).** The corporate pattern, and it would give per-user accounts with central revocation. It makes every master's login depend on FreeIPA reachability from five regions, adds a directory client to hosts that are being retired, and solves a problem the fleet does not have, since nobody logs in over sshd. Rejected for this fleet.
- **SSH CA or a bastion.** Rejected in ADR 0032, nothing here changes that. Identity Center groups were not considered there and are not considered here: SSM Session Manager already carries the operator identity.

## Rollout

- Seed the parameter with the eight committed slugs.
- Land the reconciler, the association, and the docs in one PR. The module variable defaults to off and each `master-*.tf` opts in explicitly, so the commit history records exactly which masters are enabled at every point of the rollout. `ssh_key_engineers` and `setup_ssh_keys` are removed in the commit after the whole fleet converged and pruned, with the doc sweep and the alerts following.
- Canary on `ps80-upgraded` (the only master opted in at first), then `ps80`, then batches of three, each batch its own commit and its own targeted apply. Per master: association `Success`, `sshd -t` clean, `sshd -T` lists both key files and the unchanged EC2 Instance Connect command, a throwaway key added to the parameter authenticates over the SSM tunnel and stops after removal.
- Fingerprint inventory before the prune: on 2026-09-14 all ten EC2 instances held exactly ten keys, nine matching the keys percona.com serves for the eight slugs and one matching the shared `percona-jenkins` key pair. The reconciler now enforces the same check mechanically before every prune.
