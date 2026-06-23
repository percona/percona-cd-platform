# docs/

Index of design notes, runbooks, and ADRs for the platform.

## Start here

| Topic | Doc |
|---|---|
| Architecture: layered views, failure domains, decisions, open questions | [`architecture.md`](architecture.md) |
| PoC history & lessons | [`poc-history.md`](poc-history.md), [`lessons-from-poc.md`](lessons-from-poc.md) |
| CI antipatterns: what not to do on the platform, and why | [`ci-antipatterns.md`](ci-antipatterns.md) |

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
| Red-team reviews | [`security-review-2026-06-11.md`](security-review-2026-06-11.md) (Authentik posture), [`security-review-2026-05-07.md`](security-review-2026-05-07.md) |

## Networking

| Topic | Doc |
|---|---|
| TLS strategy | [`tls-strategy.md`](tls-strategy.md) |
| Connectivity: address plan, peering, request paths, SGs, DNS | [`connectivity.md`](connectivity.md) |

## Runbooks

Step-by-step procedures for operational tasks.

- [`add-jenkins-host.md`](runbooks/add-jenkins-host.md): bring a new Jenkins host onto the shared ALB
- [`authentik-blueprint-ops.md`](runbooks/authentik-blueprint-ops.md): manage Authentik blueprints (export, apply, troubleshoot)
- [`authentik-bootstrap.md`](runbooks/authentik-bootstrap.md): Authentik first-time configuration
- [`authentik-cert-rotation.md`](runbooks/authentik-cert-rotation.md): rotate Authentik signing certificates
- [`bootstrap-state.md`](runbooks/bootstrap-state.md): pre-creating the state bucket + lock
- [`decommission-ps3-ec2-master.md`](runbooks/decommission-ps3-ec2-master.md): retire the ps3 EC2 spot master, re-parent its substrate (done 2026-06-07)
- [`disaster-recovery.md`](runbooks/disaster-recovery.md): cluster recovery procedures
- [`common-operations.md`](runbooks/common-operations.md): the day-to-day changes (worker templates, sizing, keys, ports), ranked by real frequency
- [`argocd-admin-recovery.md`](runbooks/argocd-admin-recovery.md): ArgoCD admin break-glass when SSO is down
- [`break-glass-dns.md`](runbooks/break-glass-dns.md): break-glass DNS for the Jenkins master web plane when the ALB path is down
- [`eks-upgrade.md`](runbooks/eks-upgrade.md): control plane + node group upgrades
- [`eks-api-access.md`](runbooks/eks-api-access.md): obtain kube API access (access entries, `authenticationMode=API`)
- [`grafana-saml-cutover.md`](runbooks/grafana-saml-cutover.md): Grafana OAuth cutover
- [`jenkins-ssl-cutover.md`](runbooks/jenkins-ssl-cutover.md): per-master SSL cutover to the shared ALB
- [`jenkins-mcp-exports.md`](runbooks/jenkins-mcp-exports.md): jenkins-mcp log/artifact S3 export tools (presigned downloads)
- [`jenkins-mcp-operate.md`](runbooks/jenkins-mcp-operate.md): operate the jenkins-mcp gateway fleet (add/remove a master, onboard a writer)
- [`jenkins-mcp-image-autodeploy.md`](runbooks/jenkins-mcp-image-autodeploy.md): the jenkins-mcp image build + auto-deploy pipeline (ADR 0038)
- [`lgtm-az-migration.md`](runbooks/lgtm-az-migration.md): relocate bound LGTM PVCs across AZs
- [`lgtm-orphan-pvc-sweep.md`](runbooks/lgtm-orphan-pvc-sweep.md): clean up orphaned LGTM PVCs
- [`cleanup-reapers.md`](runbooks/cleanup-reapers.md): operate the volume + EC2 cleanup reapers (arm/disarm, tags, dry-run)
- [`orphaned-snapshot-cleanup.md`](runbooks/orphaned-snapshot-cleanup.md): clean up orphaned EBS snapshots from retired masters
- [`master-shell-access.md`](runbooks/master-shell-access.md): shell on a master (`just ssh`, SSM, EIP allow-list, ps3 kubectl)
- [`migrate-ps3-to-eks.md`](runbooks/migrate-ps3-to-eks.md): moving a Jenkins master in-cluster (ps3 done, the EC2 master retired, see `decommission-ps3-ec2-master.md`)
- [`mng-label-taint-changes.md`](runbooks/mng-label-taint-changes.md): apply MNG label/taint edits without a drain
- [`restore-mimir.md`](runbooks/restore-mimir.md): restore Mimir state from S3
- [`rotate-acm.md`](runbooks/rotate-acm.md): rotate the ACM wildcard cert (stub)

## ADRs

Architecture decision records, numbered chronologically in [`adr/`](adr/).
Propose new architecture decisions there before changing the code.
