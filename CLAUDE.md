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
- **Docker image:** always `docker buildx build --platform linux/amd64 --push`. Build host (m3) is arm64, EKS nodes are amd64.
- **Public repo:** no AWS account IDs / ARNs / secrets in `.tfvars` or values files. All sensitive bits flow via `var.account_id`, cluster-secret annotations, or AWS Secrets Manager → External Secrets Operator.
- **TLS policy:** every ALB Ingress sets `alb.ingress.kubernetes.io/ssl-policy: ELBSecurityPolicy-TLS13-1-2-Res-PQ-2025-09` (AWS's recommended default since 2025-09 — TLS 1.3+1.2 only, post-quantum hybrid key exchange, forward-secrecy-only ciphers). Defaults allow TLS 1.0/1.1.
- **Shared ALB:** every Jenkins host Ingress carries `alb.ingress.kubernetes.io/group.name: jenkins-cd`.
- **Pre-commit + CI:** `just ci` is the gate. Same set runs on PR.
- **Terraform only via `just tf-*`.** The justfile is the single TF entrypoint; no raw `tofu`, no `cd terraform`. Recipes: `tf-plan` (writes `tfplan`), `tf-apply` (applies the saved `tfplan`, never auto-approve — `tf-apply-now` is removed), `tf-state-backup` before risky applies, `tf-state-versioning-check`, `tf-plan-masters` (PLAN-ONLY; `-target`/`-exclude` are plan-only, no `tf-apply-masters`).
- **`AWS_PROFILE` from env, never baked.** AWS-touching recipes require it and fail loudly if unset. Do NOT set `aws_profile` in `terraform/local.auto.tfvars` (that file is local/gitignored; `providers.tf` falls through to the SDK chain when the var is empty — setting it there reintroduces the profile split-brain).

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

## Hot-button gotchas

1. **EKS extended support.** Standard support: 1.33 / 1.34 / **1.35 (default)**. Picking 1.32 or below incurs paid extended-support fees.
2. **EBS-CSI volume zonality.** EBS is per-AZ. StatefulSets that need volume-follows-pod must pin to one AZ via `nodeSelector` + StorageClass `allowedTopologies`. Multi-AZ HA needs EFS (slower).
3. **Pod Identity needs the agent.** `eks-pod-identity-agent` managed addon is mandatory; without it every association silently no-ops.
4. **EC2-plugin IRSA classloader bug.** Jenkins EC2 plugin (AWS SDK v1) has classloader isolation that breaks `DefaultCredentialsProvider`. Patched fork `ec2:5.24.percona.2` + `e-ec2-irsa-credential.groovy` are the only working path. Pod Identity *should* fix it transparently — verify on `ps3-k8s` (the first in-cluster master) before claiming so.
5. **Karpenter taint exclusion.** Stateful NGs (`prometheus-system`, `jenkins-system`) carry `workload=<x>:NoSchedule`. Default Karpenter NodePool must `NotIn` that taint.
6. **EKS hardening checklist.** Before uncommenting `terraform/eks.tf` / `eks-addons.tf` modules, work through `docs/eks-hardening.md` (Top-5: access entries with `authenticationMode=API`, `publicAccessCidrs` allowlist, control-plane logging, VPC CNI prefix delegation, addon-version pinning).
7. **`percona-dev-admin` cleanup tags.** Two tags are mandatory on all resources or AWS-side cleanup Lambdas wipe them: `iit-billing-tag` (any value — EC2 cleanup terminates instances missing this tag after 10 min) and `PerconaKeep=True` (capital P, capital K — volume cleanup deletes any `available` EBS volume daily without it). Both are in `var.tags` defaults. EBS volumes provisioned by the in-cluster CSI driver carry them via `parameters.tagSpecification_*` in `resources/addons/storageclass-gp3/templates/storageclasses.yaml`.
8. **Memberlist `cluster_label` isolation.** Mimir/Loki/Tempo charts default `cluster_label_verification_disabled: true`; without a unique `cluster_label` per stack, gossip cross-pollinates and Loki ingester pods land in Mimir's `collectors/ring`, breaking PromQL with `cortex.Ingester Unimplemented`. Set per-stack labels in `resources/addons/{mimir,loki,tempo}/values.yaml`. See ADR 0014.
9. **Loki + S3 Object Lock incompatibility.** Loki's bundled AWS SDK Go v1 doesn't emit `Content-MD5` / `x-amz-checksum-*` on PutObject, so an Object-Lock-enabled bucket rejects every write and ingesters never flush or become Ready. Use lifecycle expiration for retention, not Object Lock. Mimir (SDK v2 via Thanos objstore) is unaffected; Tempo's SDK is at the same risk class. See ADR 0015 and commit `7df2f57`.
10. **Loki ingester ring resilience.** Set `loki.loki.ingester.autoforget_unhealthy: true` in `resources/addons/loki/values.yaml`. Without it, ungraceful pod terminations pin `UNHEALTHY` ring entries forever and the lifecycler readiness gate never flips Ready; distroless containers make manual `forget` POSTs fragile. Same commit `7df2f57`.
11. **Push URL + master-label conventions.** `prometheus.receive_http` (the alloy-gateway receiver) accepts `/api/v1/metrics/write`, NOT Mimir's distributor-native `/api/v1/push` (easy 404). Use `master="<inst>.cd"` for EC2 masters and `master="<inst>-k8s"` for in-cluster POC; bare `master="ps3"` collides between ps3.cd and ps3-k8s and triggers `err-mimir-sample-out-of-order`. See ADR 0013 amendments and the `percona-observability` skill.
12. **alloy-gateway auth split.** The NGINX bearer-auth sidecar strips the `Authorization` header before proxying loopback to inner Alloy receivers. Two-bearer split (separate AWS SM secrets `…/alloy-gateway/bearer` and `…/alloy-gateway/worker-bearer`) recommended for masters vs workers so leak blast-radius is bounded.
13. **EC2 master cloud-config (push pipeline).** Every `jenkins-<inst>-master` role carries `AmazonSSMManagedInstanceCore` (managed) + scoped `AlloyGatewayBearerRead` (inline) via CFN/Terraform (`Percona-Lab/jenkins-pipelines` PR #4037, commit 83adb97, merged 2026-05-10). Bearer secret value at `percona-ci-platform/alloy-gateway/bearer` is JSON `{"bearer_token":"..."}` (not plain string); every consumer must JSON-parse before use. Master-side `/usr/local/bin/alloy-fetch-token` runs via systemd `ExecStartPre=+/usr/local/bin/alloy-fetch-token` -- the `+` prefix elevates to root because `/etc/alloy/` is `0750 root:alloy` while alloy itself runs `User=alloy`. Token rotation is `systemctl restart alloy` per master; the fetcher re-runs on every alloy start.
14. **MNG label/taint changes via AWS CLI, not tofu rolling drain.** Changing `labels` or `taints` on an EKS managed node group in `terraform/eks.tf` triggers the AWS provider to issue an `update-nodegroup-version` (rolling drain). On single-replica stateful MNGs (`prometheus_system`, `jenkins_system`) the drain fails with `PodEvictionFailure` when the resident workload's PDB blocks eviction (Bitnami Authentik PG / Grafana / Jenkins master). The drain takes ~25 min to time out, then tofu errors and leaves the MNG in a half-updated state. **Prevention:** for label/taint-only changes, use `aws eks update-nodegroup-config --labels ... --taints ...` (issues a `ConfigUpdate` not a `VersionUpdate` -- in-place, no node roll). Then `tofu apply -refresh-only -auto-approve` so state matches live. The eks.tf still represents the source of truth; the CLI just bypasses tofu's drain wait. Use tofu for genuine AMI bumps (where rolling is intended) -- pair those with `force_update_version = true` on the resource when the resident PDB is known to block. See `docs/runbooks/mng-label-taint-changes.md`.

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
if any expected master is missing from Mimir.

`scripts/check-master-spot-readiness.sh [<inst>]`
Spot-interrupt readiness audit (SpotFleet + Capacity Rebalancing, cron +
graceful-stop.sh with flock, rehydrate flag, Secrets Manager + api-admin
auth probe). Use before declaring a master ready to absorb a spot interrupt.

## Related repos

| Repo | Purpose |
|---|---|
| `Percona-Lab/jenkins-pipelines` | Jenkins pipeline code, cloud.groovy, job definitions (master + hetzner branches) |
| `nogueiraanderson/hetzner-cloud-plugin` | Patched Hetzner plugin (`v103.percona.11`, DC breakers, type fallback, `/hetzner-prometheus` `UnprotectedRootAction` for the push model) |
| `nogueiraanderson/ec2-plugin` | Patched EC2 plugin (`v5.24.percona.2` — IRSA classloader fix, NPE guards) |

## Skill loading reminders

When working in this repo, prefer:
- `tofu` (not `terraform`) — see global CLAUDE.md OpenTofu rules.
- `paws` for AWS lookups (load `/paws` skill first), not raw `aws` ad-hoc.
- `jenkins` CLI for Jenkins fleet ops (load `/percona-jenkins` first).
