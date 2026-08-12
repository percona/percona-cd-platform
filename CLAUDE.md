# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

# percona-cd-platform — Claude Code project instructions

OpenTofu + ArgoCD platform repo. EKS in `us-east-1`, GitOps-bootstrapped. Public, lives at `Percona/percona-cd-platform` (moved 2026-05-18). The provisioned cluster keeps its name `percona-ci-platform` and the LGTM S3 buckets keep `percona-ci-platform-*` prefixes; those names don't track the repo move.

Full architecture: see [`README.md`](README.md). Authoritative plan: private at `~/.claude/plans/spicy-prancing-nebula.md`.

## Conventions (carried forward from PoC, hard-won)

- **All changes via code.** No manual `kubectl annotate`, `kubectl patch`, or Script Console mutations. Drift between git and cluster breaks GitOps.
- **Persistent groovy scripts** (`images/jenkins/groovy/persistent/`) run every Jenkins startup after `cloud.groovy`. Alphabetical order matters; prefix with `e-` to run after `c-cloud.groovy`.
- **One-time groovy scripts** (`images/jenkins/groovy/one-time/`) self-delete after running, write `.clone-initialized` flag.
- **`persistence.volumes`, not `extraVolumes`** for ConfigMap mounts. Init containers can't see `extraVolumes`.
- **Docker image:** build multi-arch with `docker buildx build --platform linux/amd64,linux/arm64 --push` (needs a docker-container builder, not the default driver). Build host (m3) is arm64; EKS nodes are arm64 (Graviton), and the internal images are multi-arch (CI builds them via `--platform=$BUILDPLATFORM` cross-compile).
- **Public repo:** no AWS account IDs / ARNs / secrets in `.tfvars` or values files. All sensitive bits flow via `var.account_id`, cluster-secret annotations, or AWS Secrets Manager → External Secrets Operator.
- **TLS policy:** every ALB Ingress sets `alb.ingress.kubernetes.io/ssl-policy: ELBSecurityPolicy-TLS13-1-2-Res-PQ-2025-09` (AWS's recommended default since 2025-09 — TLS 1.3+1.2 only, post-quantum hybrid key exchange, forward-secrecy-only ciphers). Defaults allow TLS 1.0/1.1.
- **Shared ALB:** every Jenkins host Ingress carries `alb.ingress.kubernetes.io/group.name: jenkins-cd`.
- **Pre-commit + CI:** `just ci` is the gate. Same set runs on PR. If the local pre-commit git-hook is broken (e.g. an `InvalidManifestError` from a corrupted hook cache that survives `pre-commit clean`), run `just ci` directly and commit with `--no-verify` — the hook plumbing is the broken part, not the checks. Second known-broken mode: the `terraform_validate` hook shells the `terraform` binary (not tofu) and only fires when `.tf` files are staged; an old local install (Terraform 1.5.7 vs `required_version >= 1.11`) blocks the commit even though `just tf-validate` passes — same remedy, real gates then `--no-verify`. Never use `--no-verify` to skip a *failing* check.
- **Terraform only via `just tf-*`.** The justfile is the single TF entrypoint; no raw `tofu`, no `cd terraform`. Recipes: `tf-plan` (writes `tfplan`), `tf-apply` (applies the saved `tfplan`, never auto-approve — `tf-apply-now` is removed), `tf-state-backup` before risky applies, `tf-state-versioning-check`, `tf-plan-masters` (PLAN-ONLY; `-target`/`-exclude` are plan-only, no `tf-apply-masters`).
- **`AWS_PROFILE` from env, never baked.** AWS-touching recipes require it and fail loudly if unset. Do NOT set `aws_profile` in `terraform/local.auto.tfvars` (that file is local/gitignored; `providers.tf` falls through to the SDK chain when the var is empty — setting it there reintroduces the profile split-brain).
- **Terraform conventions are gated.** The file-naming grammar, per-team `# Owner:` banners, and comment rules (no copyright headers, no `CLAUDE.md` refs, no Jira ticket IDs in comments) live in [`terraform/CLAUDE.md`](terraform/CLAUDE.md), enforced fail-closed by `scripts/check_conventions.py` — part of `just ci` (standalone: `just tf-conventions`). Run it whenever working on terraform.

## Common commands

The justfile is the single entrypoint. Export `AWS_PROFILE` first (e.g. `export AWS_PROFILE=percona-dev-admin`); `just` (no args) lists everything.

```text
just ci                         # lint + validate; no AWS creds, mirrors CI
just tf-plan                    # tofu plan -> terraform/tfplan
just tf-apply                   # apply the saved tfplan (never auto-approve)
just tf-plan-masters            # PLAN-ONLY -target sweep over master + *_arm_fleet modules
just tf-state-backup            # timestamped `tofu state pull` before a risky apply
just tf-state-versioning-check  # assert the state bucket has Object Versioning enabled
just tf-fmt | tf-fmt-check      # format | check-format (repo root)
just kubeconfig                 # update kubeconfig for the cluster
just build-image <name> [tag]   # buildx --push a percona-cd/<name> image to ECR
```

Raw `tofu` / `cd terraform` is disallowed except where a runbook explicitly flags a raw-tofu exception (e.g. a `-refresh-only` state sync).

## Module + chart pins

Source of truth: `terraform/versions.tf` (`local.modules` and `local.charts`).
Verify with `scripts/check_versions.py` before any pin bump.

## Image licensing (images/)

Repo is AGPL-3.0. Every `images/<x>/` ships **LICENSE + NOTICE + README.md**, and the Dockerfile must **`COPY` them into the image** (`/licenses/`) — repo files alone do not satisfy redistribution; the shipped image must carry them. Model: `images/jenkins/`.

- **Our code -> AGPL-3.0** (verbatim repo-root `LICENSE`); never relicense.
- **Redistributed upstream binary -> its own license** (e.g. `snapscheduler` = backube AGPL-3.0-or-later). Permissive bases/deps (MIT/Apache/BSD/PSF/LGPL) combine in but keep their terms.
- **NOTICE** carries the exact upstream notices: MIT -> verbatim copyright + permission; **Apache-2.0 -> ship `LICENSE.apache-2.0.txt` AND state significant changes (sec 4(b))**; LGPL -> name it `LGPL-3.0-only` + preserve notices. A container is a combined work at runtime (not "mere aggregation"); point to the public repo for AGPL section 13 source.
- **Do not hand-list transitive deps** (they drift). State families + key components in NOTICE; full texts ride in the image (base `/usr/share/doc/*/copyright`, Python `*.dist-info`, plugin `MANIFEST.MF`); the authoritative inventory is machine-generated (`go-licenses`, `pip-licenses`/uv, per-plugin scan).
- **Forked plugin repos** (`Percona-Lab/jenkins-{ec2,hetzner-cloud}-plugin`): preserve the upstream `LICENSE` unmodified (MIT / Apache-2.0), `NOTICE` lists Percona changes (sec 4(b)), `pom.xml <licenses>` matches upstream.

## Hot-button gotchas

1. **EKS extended support.** Standard support: 1.33 / 1.34 / **1.35 (default)**. Picking 1.32 or below incurs paid extended-support fees.
2. **EBS-CSI volume zonality.** EBS is per-AZ. StatefulSets that need volume-follows-pod must pin to one AZ via `nodeSelector` + StorageClass `allowedTopologies`. Multi-AZ HA needs EFS (slower).
3. **Pod Identity needs the agent.** `eks-pod-identity-agent` managed addon is mandatory; without it every association silently no-ops.
4. **EC2-plugin IRSA classloader bug.** Jenkins EC2 plugin (AWS SDK v1) has classloader isolation that breaks `DefaultCredentialsProvider`. Patched fork `ec2:5.24.percona.4` + `ec2-irsa-credential.groovy` are the classic EKS-fronted-master path. The in-cluster `ps3-k8s` master is wired for EKS Pod Identity instead (SA-bound, no IRSA groovy); confirm the EC2 plugin resolves creds via Pod Identity in-pod before relying on it elsewhere.
5. **Karpenter taint exclusion.** Stateful NGs (`prometheus-system`, `jenkins-system`) carry `workload=<x>:NoSchedule`. Default Karpenter NodePool must `NotIn` that taint.
6. **EKS hardening checklist.** Before uncommenting `terraform/eks.tf` / `eks-addons.tf` modules, work through `docs/eks-hardening.md` (Top-5: access entries with `authenticationMode=API`, `publicAccessCidrs` allowlist, control-plane logging, VPC CNI prefix delegation, addon-version pinning).
7. **`percona-dev-admin` cleanup tags.** Two tags are mandatory on all resources or the account's cleanup reapers (owned by THIS repo since #113: `terraform/{ec2,volume}-cleanup.tf`) wipe them: `iit-billing-tag` (EC2 reaper terminates instances missing a valid value after 10 min; numeric = unix-epoch expiry, else permanent category) and `PerconaKeep=True` (capital P, capital K — volume reaper deletes any `available` EBS volume older than 24h daily without it; `do not remove` in the Name tag also protects). Both are in `var.tags` defaults, so the reapers cannot self-reap. A third tag, `team=<owner>` (the Owner-set value: `platform` by default, overridden per master through the jenkins modules), gives per-team cost attribution that `iit-billing-tag` alone lost once a master moved in-cluster onto a shared StorageClass. Both reapers are now ARMED (`dry_run="false"`); the flip is locked in both directions by `test_dry_run_locked_to_committed_state` (changing it must update the test in the same PR). Tunables: `locals.tf` "Cleanup Lambda parameters"; ops: `docs/runbooks/cleanup-reapers.md`; design: ADR 0030. EBS volumes provisioned by the in-cluster CSI driver carry the tags via `parameters.tagSpecification_*` in `resources/addons/storageclass-gp3/templates/storageclasses.yaml`.
8. **Memberlist `cluster_label` isolation.** Mimir/Loki/Tempo charts default `cluster_label_verification_disabled: true`; without a unique `cluster_label` per stack, gossip cross-pollinates and Loki ingester pods land in Mimir's `collectors/ring`, breaking PromQL with `cortex.Ingester Unimplemented`. Set per-stack labels in `resources/addons/{mimir,loki,tempo}/values.yaml`. See ADR 0014.
9. **Loki + S3 Object Lock incompatibility.** Loki's bundled AWS SDK Go v1 doesn't emit `Content-MD5` / `x-amz-checksum-*` on PutObject, so an Object-Lock-enabled bucket rejects every write and ingesters never flush or become Ready. Use lifecycle expiration for retention, not Object Lock. Mimir (SDK v2 via Thanos objstore) is unaffected; Tempo's SDK is at the same risk class. See ADR 0015 and commit `7df2f57`.
10. **Loki ingester ring resilience.** Set `loki.loki.ingester.autoforget_unhealthy: true` in `resources/addons/loki/values.yaml`. Without it, ungraceful pod terminations pin `UNHEALTHY` ring entries forever and the lifecycler readiness gate never flips Ready; distroless containers make manual `forget` POSTs fragile. Same commit `7df2f57`.
11. **Push URL + master-label conventions.** `prometheus.receive_http` (the alloy-gateway receiver) accepts `/api/v1/metrics/write`, NOT Mimir's distributor-native `/api/v1/push` (easy 404). Use `master="<inst>.cd"` for EC2 masters and `master="<inst>-k8s"` for in-cluster controllers. Historically bare `master="ps3"` collided between the EC2 `ps3.cd` and the in-cluster `ps3-k8s` and triggered `err-mimir-sample-out-of-order`; the EC2 `ps3` was decommissioned 2026-06-07, so `ps3.cd` is now served solely by the in-cluster pod. Keep the distinct labels per [ADR 0013](docs/adr/0013-push-from-masters-with-nginx-bearer.md) amendments and the `percona-observability` skill. See [`docs/runbooks/decommission-ps3-ec2-master.md`](docs/runbooks/decommission-ps3-ec2-master.md).
12. **alloy-gateway auth split.** The NGINX bearer-auth sidecar strips the `Authorization` header before proxying loopback to inner Alloy receivers. Two-bearer split (separate AWS SM secrets `…/alloy-gateway/bearer` and `…/alloy-gateway/worker-bearer`) recommended for masters vs workers so leak blast-radius is bounded.
13. **EC2 master cloud-config (push pipeline).** Every `jenkins-<inst>-master` role carries `AmazonSSMManagedInstanceCore` (managed) + scoped `AlloyGatewayBearerRead` (inline) via CFN/Terraform (`Percona-Lab/jenkins-pipelines` PR #4037, commit 83adb97, merged 2026-05-10). Bearer secret value at `percona-ci-platform/alloy-gateway/bearer` is JSON `{"bearer_token":"..."}` (not plain string); every consumer must JSON-parse before use. Master-side `/usr/local/bin/alloy-fetch-token` runs via systemd `ExecStartPre=+/usr/local/bin/alloy-fetch-token` -- the `+` prefix elevates to root because `/etc/alloy/` is `0750 root:alloy` while alloy itself runs `User=alloy`. Token rotation is `systemctl restart alloy` per master; the fetcher re-runs on every alloy start.
14. **MNG label/taint changes via AWS CLI, not tofu rolling drain.** Changing `labels` or `taints` on an EKS managed node group in `terraform/eks.tf` triggers the AWS provider to issue an `update-nodegroup-version` (rolling drain). On single-replica stateful MNGs (`prometheus_system`, `jenkins_system`) the drain fails with `PodEvictionFailure` when the resident workload's PDB blocks eviction (Bitnami Authentik PG / Grafana / Jenkins master). The drain takes ~25 min to time out, then tofu errors and leaves the MNG in a half-updated state. **Prevention:** for label/taint-only changes, use `aws eks update-nodegroup-config --labels ... --taints ...` (issues a `ConfigUpdate` not a `VersionUpdate` -- in-place, no node roll). Then `tofu apply -refresh-only -auto-approve` so state matches live. The eks.tf still represents the source of truth; the CLI just bypasses tofu's drain wait. Use tofu for genuine AMI bumps (where rolling is intended) -- pair those with `force_update_version = true` on the resource when the resident PDB is known to block. See `docs/runbooks/mng-label-taint-changes.md`.
15. **EC2 Fleet plugin task resubmit is harmful for pipeline fleets.** With `disableTaskResubmit=false` the plugin reacts to any agent disconnect (spot reclaim included) by interrupting the running pipeline branch with ABORTED (defeating `retry(conditions: [agent()])`) and re-scheduling the WorkflowJob with `getLastBuild()` params and no cause (ghost builds that NPE on `getBuildCauses()[0]`). Keep `disableTaskResubmit: true` on every arm fleet: the eight EC2 masters set it in `resources/jenkins-masters/<inst>/init.groovy.d/ec2FleetCloud.groovy`, ps3-k8s in the clouds catalog (edit `resources/jenkins/clouds-catalog/masters/ps3.yaml`, then `render-clouds.py apply ps3`; never hand-edit the rendered values). The orphaned `resources/jenkins-masters/ps3/` tree (EC2 ps3 leftover, no terraform consumer since PS-11206) was deleted in PR 120; ps3-k8s's clouds come only from the catalog. pg's fleet cloud is still delivered from `Percona-Lab/jenkins-pipelines` (the one remaining CFN master); carry the flag there too. See PS-11265.

16. **Legacy IAM users: import, never re-create, and keep their keys out of Terraform.** The long-lived CI users predate this repo and their access keys are live Jenkins credentials. Import the user and its inline policies with `import` blocks; never declare an `aws_iam_access_key`, which cannot carry an existing secret, so a replace would break the stored credential. Managed-policy attachments need no import because `AttachUserPolicy` is idempotent. Gate the plan on `0 to destroy` with no policy-document diff, and expect a tags-only change from `default_tags`. Model: `terraform/iam-jenkins-spawn-user.tf`.

## End-to-end verification

Full script catalog: [`scripts/README.md`](scripts/README.md).

`scripts/verify-observability.sh [<inst>]`
Walks the whole LGTM push pipeline per master (Hetzner-plugin endpoint,
master-side Alloy systemd, ALB + bearer, alloy-gateway pods, Mimir + Loki
distributors + canary queries, Grafana). Default master `ps3`,
`--skip-master` for cluster-side only.

`scripts/check-master-ingest.sh`
Fleet-wide ingest spot-check from the cluster side (no SSH). Port-forwards
`mimir-query-frontend` + `loki-query-frontend`. Reports per-master series
count, sample freshness, metric cardinality, Loki line count. Exits non-zero
if any expected master is missing from Mimir. Master list is hardcoded in
the script.

`scripts/check-master-alloy-mimir.py` (`just check-master-alloy`)
Every-active-master Mimir-via-Alloy freshness gate. Enumerates the master
set dynamically from repo source-of-truth (k8s / tf-managed / cf-managed)
so the expected list cannot rot as the fleet changes; STALE/MISSING exits
non-zero.

`scripts/check-master-spot-readiness.sh [<inst>]`
Spot-interrupt readiness audit (SpotFleet + Capacity Rebalancing, cron +
graceful-stop.sh with flock, rehydrate flag, Secrets Manager + api-admin
auth probe). Use before declaring a master ready to absorb a spot interrupt.

## Related repos

| Repo | Purpose |
|---|---|
| `Percona-Lab/jenkins-pipelines` | Jenkins pipeline code, cloud.groovy, job definitions (master + hetzner branches) |
| `Percona-Lab/jenkins-hetzner-cloud-plugin` | Patched Hetzner plugin (`v103.percona.28`; DC breakers, type fallback, `/hetzner-prometheus` `UnprotectedRootAction`, `SSHUserPrivateKey`-interface fix for external cred providers). |
| `Percona-Lab/jenkins-ec2-plugin` | Patched EC2 plugin (`v5.24.percona.4`; IRSA classloader fix, NPE/CRW guards). |

## Skill loading reminders

When working in this repo, prefer:
- `tofu` (not `terraform`) — see global CLAUDE.md OpenTofu rules.
- `paws` for AWS lookups (load `/paws` skill first), not raw `aws` ad-hoc.
- `jenkins` CLI for Jenkins fleet ops (load `/percona-jenkins` first).
