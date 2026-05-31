# docs/

Index of design notes, runbooks, and ADRs for the platform.

## Start here

| Topic | Doc |
|---|---|
| Architecture overview | [`architecture.md`](architecture.md) |
| PoC history & lessons | [`poc-history.md`](poc-history.md), [`lessons-from-poc.md`](lessons-from-poc.md) |

## Subsystems

| Topic | Doc |
|---|---|
| Observability (Grafana + Mimir + Loki + Tempo) | [`observability.md`](observability.md) |
| Karpenter | [`karpenter.md`](karpenter.md) |
| ArgoCD bootstrap | [`argocd-bootstrap.md`](argocd-bootstrap.md) |
| Jenkins fleet scrape | [`jenkins-fleet-scrape.md`](jenkins-fleet-scrape.md) |
| EC2 Jenkins master resilience (spot drain + worker rehydrate) | [`ec2-master-resilience.md`](ec2-master-resilience.md) |

## Identity, access, security

| Topic | Doc |
|---|---|
| Authentication (Duo SAML → Authentik → OIDC) | [`authentication.md`](authentication.md) |
| Pod Identity (vs IRSA) | [`pod-identity.md`](pod-identity.md) |
| EKS hardening | [`eks-hardening.md`](eks-hardening.md) |
| Red-team review | [`security-review-2026-05-07.md`](security-review-2026-05-07.md) |

## Networking

| Topic | Doc |
|---|---|
| TLS strategy | [`tls-strategy.md`](tls-strategy.md) |
| Connectivity | [`connectivity.md`](connectivity.md) |

## Runbooks

Step-by-step procedures for operational tasks.

- [`add-jenkins-host.md`](runbooks/add-jenkins-host.md) — bring a new Jenkins host onto the shared ALB
- [`authentik-blueprint-ops.md`](runbooks/authentik-blueprint-ops.md) — manage Authentik blueprints (export, apply, troubleshoot)
- [`authentik-bootstrap.md`](runbooks/authentik-bootstrap.md) — Authentik first-time configuration
- [`authentik-cert-rotation.md`](runbooks/authentik-cert-rotation.md) — rotate Authentik signing certificates
- [`bootstrap-state.md`](runbooks/bootstrap-state.md) — pre-creating the state bucket + lock
- [`disaster-recovery.md`](runbooks/disaster-recovery.md) — cluster recovery procedures
- [`eks-upgrade.md`](runbooks/eks-upgrade.md) — control plane + node group upgrades
- [`grafana-saml-cutover.md`](runbooks/grafana-saml-cutover.md) — Grafana OAuth cutover
- [`jenkins-ssl-cutover.md`](runbooks/jenkins-ssl-cutover.md) — per-master SSL cutover to the shared ALB
- [`lgtm-az-migration.md`](runbooks/lgtm-az-migration.md) — relocate bound LGTM PVCs across AZs
- [`lgtm-orphan-pvc-sweep.md`](runbooks/lgtm-orphan-pvc-sweep.md) — clean up orphaned LGTM PVCs
- [`migrate-ps3-to-eks.md`](runbooks/migrate-ps3-to-eks.md) — moving a Jenkins master in-cluster
- [`mng-label-taint-changes.md`](runbooks/mng-label-taint-changes.md) — apply MNG label/taint edits without a drain
- [`restore-mimir.md`](runbooks/restore-mimir.md) — restore Mimir state from S3
- [`rotate-acm.md`](runbooks/rotate-acm.md) — rotate the ACM wildcard cert (stub)

## ADRs

Architecture decision records, numbered chronologically in [`adr/`](adr/).
Propose new architecture decisions there before changing the code.
