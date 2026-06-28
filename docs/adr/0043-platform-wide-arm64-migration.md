# 0043 - Platform-wide arm64 (Graviton) migration

**Status:** Accepted (2026-06-27)
**Extends:** [ADR 0042](0042-lgtm-stateful-arm64.md): the `lgtm-stateful` Karpenter NodePool was the first phase; this ADR carries the same approach across the rest of the cluster (the `default` and `ingress` NodePools, all three managed node groups, and the internal image program).
**Amends:** [ADR 0008](0008-managed-ng-for-stateful-system-workloads.md) and [ADR 0017](0017-cluster-tier-taxonomy-and-lgtm-pinning.md) (the `system`, `prometheus_system`, `jenkins_master` MNGs move from `m6a` to Graviton); [ADR 0027](0027-baked-jenkins-controller-image.md) (the jenkins-percona image becomes multi-arch).

## Context

After ADR 0042 moved `lgtm-stateful` to Graviton, the rest of the cluster was still amd64: the `default` and `ingress` Karpenter NodePools and all three managed node groups (`system`, `prometheus_system`, `jenkins_master`, all `m6a`). Graviton3 is about 19% cheaper per vCPU/RAM than the equivalent Intel in us-east-1 on-demand, and the lgtm phase proved the pattern (arch-aware AL2023 alias, in-AZ EBS re-attach, multi-arch upstream images).

The gating fact: three node groups host an internal ECR image, and a pool cannot move until the image it hosts is multi-arch. So the image work is the long pole and runs first. The four pre-existing internal images (`jenkins-mcp`, `mtr-ingest`, `jenkins-endpoint-reconciler`, `jenkins-percona`) were amd64-only; their `uv`/jenkins base pins were single-arch child digests, and their CI built `--platform linux/amd64`.

## Decision

1. **Make all internal images multi-arch.** Repin each base to its multi-arch INDEX digest, and convert every build to `linux/amd64,linux/arm64` in CI (split a single-arch `--load` smoke per arch, then one multi-arch `--push` with SBOM + provenance). Two operator-built images (`mtr-ingest`, `jenkins-endpoint-reconciler`) gained CI workflows + scoped GitHub-OIDC push roles.
2. **Flip the `default` and `ingress` NodePools to arm64** (GitOps YAML): arch `[amd64]`→`[arm64]`, families to Graviton (`c7g/m7g/r7g/c8g/m8g/r8g` for default; `t4g.medium` + Graviton large for ingress). Both auto-roll Drifted nodes within their disruption budget (no manual drain, unlike lgtm's `Drifted: 0`).
3. **Flip the three MNGs to arm64** (`terraform/locals.tf` + `terraform/eks.tf`): `ami_type = "AL2023_ARM_64_STANDARD"` + multi-family Graviton `instance_types` (`m7g/m8g.large`, `m7g/m8g.xlarge`). The pinned `ami_release_version` is kept (the same dated release publishes an arm64 build).
4. **Build snapscheduler in-house.** The backube `snapscheduler` operator (daily controller-PVC VolumeSnapshot) ships **amd64-only** upstream, so it was built multi-arch from the pinned source at `images/snapscheduler/` and pushed to ECR, and its hard `nodeSelector: kubernetes.io/arch: amd64` was cleared to `arm64`.

### Two findings that shape the mechanics

- **An MNG `ami_type` arch flip is a node-group REPLACEMENT, not an in-place update.** `ami_type` is ForceNew, so `tofu plan` shows `-/+`. It is SAFE because the terraform-aws-eks module sets `create_before_destroy` + `node_group_name_prefix`: the new arm64 group is healthy before the old drains. So the NO-GO gate keys on the replacement ORDER (create-before-destroy safe, destroy-then-create not), not on "any replacement". See `docs/runbooks/mng-label-taint-changes.md`.
- **Cross-compile, never emulate, for the multi-arch builds.** Building an amd64 Go toolchain under QEMU on an arm64 build host segfaults the compiler. The Dockerfiles pin the builder to `$BUILDPLATFORM` and Go cross-compiles to `$TARGETARCH`; only the runtime smoke runs the other arch under QEMU.

## Rollout

Phased, lowest-risk first, each verified before the next (PRs #300-#319). NodePool flips auto-roll; MNG flips are create-before-destroy, and the two stateful MNGs ran in a window with a pre-roll EBS snapshot of every PVC:

- `system` (hosts karpenter): rolled cleanly, the new arm64 group came up first.
- `prometheus_system`: Grafana + Authentik PG evicted gracefully (PDB allows 1); mtr-pg recovered from its re-attached 1a PVC.
- `jenkins_master`: the singleton ps3-k8s controller rescheduled onto arm64, 100Gi Retain PVC re-attached in-AZ (a transient Multi-Attach self-resolved).

## Consequences

- **Cost:** about 19% off the compute for every pool and MNG (on top of ADR 0042's lgtm saving). Storage unchanged.
- **One new owned image.** snapscheduler is now a platform-built image (`images/snapscheduler`), tracking upstream releases via the `SNAPSCHEDULER_VERSION` Dockerfile ARG. A small maintenance cost for a tool with no upstream arm64.
- **A preflight gap was exposed.** snapscheduler escaped the per-pool workload inventory (it floats on a cluster-wide nodeSelector and runs as a CronJob with no steady-state pod), surfacing as a Pending pod only after the last amd64 node was gone. The follow-up is a mechanical arch-readiness gate (enumerate every image on a pool, fail if any lacks the target arch; scan for wrong-arch nodeSelector pins).

## Verification

- `kubectl get nodes -L kubernetes.io/arch`: 0 amd64, all arm64.
- `aws eks describe-nodegroup` for each MNG: `amiType = AL2023_ARM_64_STANDARD`.
- Grafana `/api/health`, Authentik `/-/health/live/`, and the ps3-k8s controller `/login` all return 200; `uname -m` in the controller container is `aarch64`.
- `docker buildx imagetools inspect <ecr>/percona-cd/<name>:<tag>` lists `linux/amd64` + `linux/arm64` for every internal image.
- snapscheduler reconciles `ps3-k8s-jenkins-home-daily` (operator logs), 0 restarts on arm64.
