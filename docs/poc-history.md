# PoC history

The platform's path from proof of concept to production, in dates. This
page is the timeline only. The technical lessons live in
[`lessons-from-poc.md`](lessons-from-poc.md), the decisions in
[`adr/`](adr/), and the procedures in [`runbooks/`](runbooks/).

## Timeline

| When | Milestone |
|---|---|
| 2026-03-28 | First commit: "Jenkins on EKS POC (ps57 lift-and-shift)". The repo starts as an experiment |
| 2026-04-30 | The bootstrap shape settles: GitOps Bridge pattern accepted ([ADR 0005](adr/0005-gitops-bridge-bootstrap.md)), Pod Identity as the default credential path ([ADR 0004](adr/0004-pod-identity-default.md)) |
| 2026-05-06 to 05-13 | Observability becomes real: distributed LGTM ([ADR 0010](adr/0010-distributed-lgtm.md)), LGTM-only metrics stack ([ADR 0016](adr/0016-lgtm-only-metrics-stack.md)), the CPU-credit outage that produced the tier taxonomy ([ADR 0017](adr/0017-cluster-tier-taxonomy-and-lgtm-pinning.md)), single-AZ collapse ([ADR 0020](adr/0020-lgtm-single-az-collapse.md)) |
| 2026-05-18 | The shared-ALB SSL termination lands for Jenkins masters ([ADR 0019](adr/0019-shared-alb-ssl-termination-for-jenkins-masters.md)), the jenkins-ingress proxy ships, and the repo moves to the Percona org as `percona-cd-platform` |
| 2026-05-29 | First CFN master migrated to Terraform: ps80 (PR 7). The jenkins-master module is born from it |
| 2026-05-31 | The fleet operating model is written down: ownership boundary ([ADR 0024](adr/0024-jenkins-fleet-ownership-boundary.md)), singleton rollout gating ([ADR 0025](adr/0025-singleton-controller-rollout-gating.md)), canary DR model ([ADR 0026](adr/0026-canary-dr-operating-model.md)), baked controller image ([ADR 0027](adr/0027-baked-jenkins-controller-image.md)) |
| 2026-06-01 | ps3.cd served from the in-cluster controller (PR 77), the first master running as a pod, after the dark-replica pilot (PR 67) and the JENKINS_HOME restore boot (PR 73) |
| 2026-06-07 | The ps3 EC2 pet is retired, its substrate re-parented ([runbook](runbooks/decommission-ps3-ec2-master.md)) |
| Early June | ps57, pxb, pxc follow ps80 onto Terraform |
| 2026-06-10 | The one-night wave: psmdb, rel, cloud, and pmm migrate (PRs 134, 136, 139, 141), the old CloudFormation stacks and VPCs are deleted, and the fleet goes EIP-less. Only pg remains on CloudFormation |
| 2026-07-07 | pg migrates to Terraform (PKG-1341), the last CloudFormation master; zero CloudFormation masters remain (PRs 360, 362, 363) |

## What the PoC proved and what it cost

The PoC phase (roughly April) established the patterns the platform still
runs on: the bootstrap chain, the shared ALB, Pod Identity, and the
all-changes-in-code discipline. Its painful lessons (the EC2 plugin's IRSA
blocker, cloud.groovy clobbering startup config, OAuth callback
mismatches, init script ordering) are recorded with their resolutions in
[`lessons-from-poc.md`](lessons-from-poc.md), and several graduated into
patched plugin forks and module design choices.

The production phase (May onward) was incident-driven hardening: each
outage became an ADR, a runbook, or a gate, which is the operating model
this repo keeps ([ADR 0026](adr/0026-canary-dr-operating-model.md)).

## What this page does not duplicate

Per-master migration mechanics (the preflight and validation gates, the
cutover quirks) live in the migration tooling and runbooks, the current
state lives in [`architecture.md`](architecture.md), and the in-cluster
target shape is tracked through
[ADR 0024](adr/0024-jenkins-fleet-ownership-boundary.md) and the ps3
proof of concept.
