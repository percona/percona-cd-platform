# percona-ci-platform — Claude Code project instructions

OpenTofu + ArgoCD platform repo. EKS in `us-east-1`, GitOps-bootstrapped. Public, owned by `nogueiraanderson` (will move to `Percona-Lab/`).

Full architecture: see [`README.md`](README.md). Authoritative plan: private at `~/.claude/plans/spicy-prancing-nebula.md`.

## Conventions (carried forward from PoC, hard-won)

- **All changes via code.** No manual `kubectl annotate`, `kubectl patch`, or Script Console mutations. Drift between git and cluster breaks GitOps.
- **Persistent groovy scripts** (`image/groovy/persistent/`) run every Jenkins startup after `cloud.groovy`. Alphabetical order matters — prefix with `e-` to run after `c-cloud.groovy`.
- **One-time groovy scripts** (`image/groovy/one-time/`) self-delete after running, write `.clone-initialized` flag.
- **`persistence.volumes`, not `extraVolumes`** for ConfigMap mounts. Init containers can't see `extraVolumes`.
- **Docker image:** always `docker buildx build --platform linux/amd64 --push`. Build host (m3) is arm64, EKS nodes are amd64.
- **Public repo:** no AWS account IDs / ARNs / secrets in `.tfvars` or values files. All sensitive bits flow via `var.account_id`, cluster-secret annotations, or AWS Secrets Manager → External Secrets Operator.
- **TLS policy:** every ALB Ingress sets `alb.ingress.kubernetes.io/ssl-policy: ELBSecurityPolicy-TLS13-1-2-2021-06`. Defaults allow TLS 1.0/1.1.
- **Shared ALB:** every Jenkins host Ingress carries `alb.ingress.kubernetes.io/group.name: jenkins-cd`.
- **Pre-commit + CI:** `just ci` is the gate. Same set runs on PR.

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

## End-to-end verification

`scripts/verify-observability.sh [<inst>]` walks the whole LGTM push pipeline (master Hetzner-plugin endpoint, master-side Alloy systemd, ALB + bearer, alloy-gateway pods, Mimir distributor + canary query, Loki distributor + ring + canary query, Grafana). Read-only, exits nonzero on any failure. Default master `ps3`. Pass `--skip-master` to omit SSH-dependent stages.

For fleet-wide ingest spot-checks from the cluster side (no SSH required), use `scripts/check-master-ingest.sh` -- it port-forwards `mimir-query-frontend` + `loki-query-frontend` and reports per-master series count, sample freshness, metric-name cardinality, and Loki log-line counts. Exits nonzero if any expected master is missing from Mimir.

## Related repos

| Repo | Purpose |
|---|---|
| `Percona-Lab/jenkins-pipelines` | Jenkins pipeline code, cloud.groovy, job definitions (master + hetzner branches) |
| `nogueiraanderson/hetzner-cloud-plugin` | Patched Hetzner plugin (`v103.percona.11`, DC breakers, type fallback, `/hetzner-prometheus` `UnprotectedRootAction` for PS-10997 push model) |
| `nogueiraanderson/ec2-plugin` | Patched EC2 plugin (`v5.24.percona.2` — IRSA classloader fix, NPE guards) |

## Skill loading reminders

When working in this repo, prefer:
- `tofu` (not `terraform`) — see global CLAUDE.md OpenTofu rules.
- `paws` for AWS lookups (load `/paws` skill first), not raw `aws` ad-hoc.
- `jenkins` CLI for Jenkins fleet ops (load `/percona-jenkins` first).
