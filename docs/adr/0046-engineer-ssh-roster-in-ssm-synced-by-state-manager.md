<!-- Copyright (C) 2026 Percona LLC -->
# 0046 - Engineer SSH roster in SSM, synced to the masters by State Manager

**Status:** Proposed (2026-09-14)
**Amends:** [ADR 0032](0032-ec2-instance-connect-over-ssm-for-operator-ssh.md), whose static-key fallback was fed by a committed `ssh_key_engineers` list written once at boot. The fallback stays, its source and delivery change.
**Related:** [ADR 0036](0036-operator-allowlists-in-ssm-and-dynamic-sso-access-entry.md) (operator allowlists already live in SSM Parameter Store with the same write path), [ADR 0013](0013-push-from-masters-with-nginx-bearer.md) (every master carries `AmazonSSMManagedInstanceCore`).

## Context

The `ssh_key_engineers` list named the same eight engineers in every `master-*.tf` of a public repo. Three defects followed:

- The public repo doubled as a targeting list of who can SSH into CI.
- A roster edit was a ten-file change that drifted.
- Keys were written once at first boot, and `aws_instance.master` ignores `user_data` changes. Revoking an engineer meant a PR, an apply, and a rebuild of each master.

The fleet does not use these keys. Over the 16 to 96 days since each master last booted, sshd on all nine accepted zero public-key logins. Operators use SSM Session Manager (`just ssh`), which never touches sshd, and the EC2 Instance Connect path was used three times in 90 days. The static keys are a break-glass fallback, and a fallback still needs a revocation path that works on a running master.

## Decision

- The roster lives in one SSM Parameter Store parameter, `/<cluster>/access/master-ssh-engineers`, Standard tier, type `String`, value `{"schema":1,"engineers":["<slug>",...]}`. One fleet-wide list, not one per master: an offboarding is one write, and today's ten lists are identical anyway. Per-master scoping, if ever needed, belongs to IAM conditions on `ec2-instance-connect:SendSSHPublicKey`, not to a key file.
- Slugs, not keys. The public keys stay published by Percona IT at `percona.com/get/engineer/KEY/<slug>.pub`, so key rotation remains theirs. The nine current keys total 4977 bytes, over the 4 KB Standard limit, which also rules out storing them in the parameter.
- JSON rather than StringList because SSM rejects an empty value, and `"engineers": []` must be expressible: it is the revoke-all state.
- Terraform only names the parameter. It is not an `aws_ssm_parameter` resource and not a data source, so the roster enters neither the repo nor the state. Writes go through `just engineers-set`, every write is a CloudTrail `PutParameter`.
- Delivery is an `aws_ssm_association` per master, the pattern the module already uses for `init.groovy.d`. It runs once on creation, then every 30 minutes, and reaches the running fleet with no rebuild. `just engineers-set` also calls `StartAssociationsOnce`, so 30 minutes is the worst case, not the target.
- The reconciler writes a dedicated file, `/etc/ssh/authorized_keys.d/ec2-user.engineers`, root-owned `0644`, named by one `AuthorizedKeysFile` drop-in in `sshd_config.d`. `~ec2-user/.ssh/authorized_keys` keeps only the EC2 key pair line, and the legacy engineer keys are pruned from it once, with a dated backup. Without the prune the new file would add keys and revoke nothing.
- Every fetched line is validated with `ssh-keygen -lf` before it is written. A 404 body is HTML and must never reach the file.
- Fail-safe over strict: on any SSM, HTTP, or validation failure the last good file stays byte-identical and the association reports failure. An explicit empty roster writes an empty file without contacting percona.com.
- No IAM change. `AmazonSSMManagedInstanceCore` already grants `ssm:GetParameter` on `*` to every master role.

## Consequences

- Revocation lands fleet-wide within one tick and needs no rebuild, no PR, and no apply.
- The roster is out of the repo and out of Terraform state. Git history, older launch-template versions, and instance user-data still hold the old lists. This decision reduces future exposure, it does not erase the past.
- The break-glass fallback depends on percona.com at sync time, as the boot script already did. During an outage stale keys persist until the next successful sync. If bounded revocation during an outage ever becomes a requirement, the alternative is a key snapshot in an Advanced-tier parameter.
- Association status is the health signal, and each run also drops a textfile metric that the master-side Alloy unix exporter scrapes (`engineer_keys_sync_last_run_success`, `engineer_keys_sync_last_success_timestamp_seconds`). Three warning alerts in the jenkins-uptime addon cover a failing run, two hours without success, and a master that never reported a run. `just ssm-run` masks the remote exit code, so verification reads the association execution status, never a shell exit code.
- A fresh fork or DR rebuild must seed the parameter before the associations run, otherwise the association reports failed convergence and the master carries no engineer keys. SSM, EC2 Instance Connect, and the EC2 key pair are unaffected either way.
- The evidence above supports retiring the static keys entirely. That is a separate decision, gated on agreement from the people on the roster and a written break-glass for the case where SSM and the EC2 API are both unavailable. Under this design it costs one write: an empty roster.

## Alternatives considered

- **Plan-time SSM data read (the ADR 0036 pattern).** Takes the roster out of the repo but puts it into state and user-data, and a change still reaches a running master only on rebuild. Rejected.
- **Gitignored tfvars.** Rejected for the reasons ADR 0036 gave: invisible to review, divergent operator copies, a checkout without the file plans a removal.
- **A boot-time reader plus a systemd timer.** Does not reach the running fleet without a rebuild. The State Manager association does, and it exposes execution results.
- **AWS Secrets Manager.** The roster is configuration, not a secret. Per-secret cost, rotation semantics nobody needs, and a VPC endpoint that does not exist yet. Rejected.
- **One parameter per master.** Recreates the duplication in SSM and turns offboarding into nine writes. Rejected.
- **SSH CA, bastion, Identity Center groups.** Rejected in ADR 0032, nothing here changes that.

## Rollout

- Seed the parameter with the eight committed slugs.
- Land the reconciler, the association, and the docs in one PR. The module variable defaults to off and each `master-*.tf` opts in explicitly, so the commit history records exactly which masters are enabled at every point of the rollout. The last commit of the same PR removes `ssh_key_engineers` and `setup_ssh_keys`, after the whole fleet has converged and pruned.
- Canary on `ps80-upgraded` (the only master opted in at first), then `ps80`, then batches of three, each batch its own commit and its own targeted apply. Per master: association `Success`, `sshd -t` clean, `sshd -T` lists both key files and the unchanged EC2 Instance Connect command, a throwaway key added to the parameter authenticates over the SSM tunnel and stops after removal.
- Fingerprint inventory before the prune: on 2026-09-14 all ten EC2 instances held exactly ten keys, nine matching the keys percona.com serves for the eight slugs and one matching the shared `percona-jenkins` key pair, which is a single key in all five regions.
