# Architecture

## One-screen view

```
                          *.cd.percona.com
                                 │
                                 ▼
                       ALB :443 (ACM wildcard)
                ┌─── Ingress group jenkins-cd ───┐
                │                                │
        Mode A (in-cluster)               Mode B (proxy → EC2)
                │                                │
                ▼                                ▼
        StatefulSet jenkins-<host>      jenkins-ingress NGINX (shared)
        (tier lgtm-stateful*,                       │
         AZ us-east-1a, gp3 PVC)                    ▼
                                          K8s Service jenkins-<host>
                                                    │
                                                    ▼
                                          EndpointSlice IP (filled by
                                          jenkins-endpoint-reconciler
                                          CronJob, every minute)
                                                    │
                                                    ▼
                                          EC2 Jenkins master :8080 in
                                          another region (peered VPC)
```

*`jenkins-system` tier was retired 2026-05-13 (no master ever claimed it).
The in-cluster `ps3` master targets the `jenkins-master` tier label, served
by the dedicated `jenkins_master` MNG (ADR 0025); see the compute-topology
table below.

## Host scope

| Host(s) | Role | Notes |
|---|---|---|
| `pmm`, `ps80`, `pxc`, `pxb`, `psmdb`, `ps57`, `rel`, `cloud` (`.cd.percona.com`) | Mode B (ALB → in-cluster NGINX → EC2 master) | Friendly DNS points at the ALB; the shared `jenkins-ingress` NGINX proxies to the per-host K8s Service, whose EndpointSlice the `jenkins-endpoint-reconciler` CronJob keeps in sync with the live EC2 master's private IP (re-runs every minute, survives instance replacement). The masters are single on-demand instances, Terraform-managed (`terraform/master-<host>.tf`), and EIP-less: the public IPv4 is subnet-auto-assigned and dynamic (shell via SSM, [`runbooks/master-shell-access.md`](runbooks/master-shell-access.md)); pxc exceptionally keeps an EIP for an inbound JNLP agent pinned to it. |
| `pg.cd.percona.com` | Legacy direct path (NOT migrated, deferred by decision) | DNS points at the master's own EIP; on-box openresty + certbot terminate TLS. Still CloudFormation (`Percona-Lab/jenkins-pipelines/IaC/pg.cd/`) on a SpotFleet. The only master left on this shape. |
| `ps3.cd.percona.com` | Mode A (in-cluster StatefulSet) | First master moved in-cluster, and the only one served directly by the pod. The shared ALB routes `ps3.cd` straight to `jenkins-ps3-k8s-0`; there is no Mode B proxy or EC2 origin in the path. Seeded from the former EC2 `ps3` via a cross-region EBS snapshot copy of `JENKINS_HOME`. See [`runbooks/migrate-ps3-to-eks.md`](runbooks/migrate-ps3-to-eks.md). |
| `grafana.cd.percona.com`, `argocd.cd.percona.com` | In-cluster Service behind the same ALB | Platform UIs. |

The `ps3` EC2 "pet" master was retired on 2026-06-07 (PS-11206): the spot
fleet was cancelled, the EC2 instance terminated, and `module.ps3` deleted
from Terraform. Its still-load-bearing substrate (the VPC, subnets, IGW,
route table, S3 gateway endpoint, worker IAM role/profile, and the ARM
Graviton SG/launch-template/ASG) was re-parented into the
`jenkins-arm-standalone` module (`terraform/master-ps3.tf` →
`module.ps3_arm_fleet`) via zero-diff `moved{}` blocks, so the in-cluster
controller keeps its `docker-32gb-aarch64` Fleet fallback over the EKS↔ps3
peering. The old EC2 `JENKINS_HOME` volume is retained (forgotten from state
with `destroy=false`, `PerconaKeep=True`), plus an EBS snapshot and an S3
archive. See [`runbooks/decommission-ps3-ec2-master.md`](runbooks/decommission-ps3-ec2-master.md).

## Ownership boundary

- **OpenTofu** owns AWS-side state up to "ArgoCD healthy."
- **ArgoCD** owns everything in-cluster from there. App-of-Apps + ApplicationSets reconcile from `resources/`.
- The cluster Secret (`argocd.argoproj.io/secret-type: cluster`) carries TF outputs (cluster name, OIDC, role ARNs, ACM ARN, Karpenter SQS) as annotations. ApplicationSets read those annotations as Helm `valuesObject`.

## Account cleanup reapers

Account hygiene, not cluster path: a daily unattached-EBS-volume reaper and a
5-minute untagged-EC2 + orphan-`eksctl`-stack reaper, both instantiated from
the `scheduled-lambda` module (caller supplies the least-privilege IAM policy;
the module owns zip, EventBridge rule, `source_arn` invoke permission, log
retention, concurrency 1). One function each in us-east-1; handlers sweep
regions in code. Tunables and the `dry_run` arming flag live in
`terraform/locals.tf`. Design: [ADR 0030](adr/0030-account-cleanup-reapers-in-terraform.md);
ops: [`runbooks/cleanup-reapers.md`](runbooks/cleanup-reapers.md).

## Compute topology

Four tiers, each tagged with `workload.percona.com/tier` and (where
exclusive) a matching taint. Two MNGs + two Karpenter NodePools.

| Tier | Capacity | AZ | Taint | Hosts |
|---|---|---|---|---|
| `bootstrap` | EKS MNG `system`, on-demand, `m6a.large × 3` | multi-AZ | `CriticalAddonsOnly=true:NoSchedule` | ArgoCD, Karpenter, AWS LB controller, external-secrets, external-dns, kube-state-metrics |
| `obs-state` | EKS MNG `prometheus_system`, on-demand, `m6a.large × 1` | us-east-1a | `workload.percona.com/tier=obs-state:NoSchedule` | Authentik Postgres, Grafana, prometheus-operator CRDs |
| `lgtm-stateful` | Karpenter NodePool, on-demand, `r7a/r7i/m7a/m7i × large/xlarge/2xlarge` | us-east-1a | `workload.percona.com/tier=lgtm-stateful:NoSchedule` | Mimir, Loki, Tempo ingesters; store-gateway; compactor; alertmanager. Pinned single-AZ for EBS-per-pod zonality. |
| `general` | Karpenter NodePool `default`, spot + on-demand, `c7/m7/r7-i/a × large…4xlarge` | us-east-1a | none (untainted fallthrough) | Stateless LGTM components, Grafana web, Authentik web, alloy-gateway, jenkins-proxy NGINX, jenkins-endpoint-reconciler |

Karpenter pools are single-AZ because the workloads need EBS volumes that
follow the pod. Multi-AZ HA there requires EFS (not provisioned).

## Storage

| StorageClass | Default | AZ | Reclaim | Used by |
|---|---|---|---|---|
| `gp3` | yes | multi-AZ | Delete | Generic stateless workloads needing scratch volumes |
| `gp3-monitoring-1a-retain` | no | us-east-1a | Retain | Grafana data, Authentik Postgres |
| `gp3-jenkins-1a-retain` | no | us-east-1a | Retain | In-cluster Jenkins master `JENKINS_HOME` |

LGTM long-term data lives in S3 (Mimir blocks, Loki chunks, Tempo
traces). Ingester ring + WAL use ephemeral EBS via `gp3`.

## ArgoCD Applications

App-of-Apps (`argocd-bootstrap/root`) fans out to ApplicationSets that
reconcile from `resources/addons/` (one app per addon) and
`resources/jenkins/master/instances/` (one app per in-cluster master).

Live apps grouped by purpose:

- **Bootstrap:** `external-secrets`, `aws-load-balancer-controller`, `external-dns`, `karpenter`, `storageclass-gp3`, `priorityclasses`, `prometheus-operator-crds`
- **Identity:** `authentik`
- **Observability:** `mimir`, `loki`, `tempo`, `grafana`, `alloy`, `alloy-gateway`, `prometheus-node-exporter`, `kube-state-metrics`
- **Jenkins:** `jenkins-ingress` (shared-ALB SSL), `jenkins-endpoint-reconciler` (EC2 → EndpointSlice IP sync), `jenkins-ps3-k8s` (first in-cluster master)

CRDs are installed via standalone Helm releases (`prometheus-operator-crds`,
the Karpenter CRD chart, the alloy operator CRDs). The umbrella charts that
depend on them apply after. `cert-manager` is intentionally out of scope, see
[`adr/0007-cert-manager-deferred.md`](adr/0007-cert-manager-deferred.md).

## EC2 master resilience

The eight Terraform-managed masters run as single ON-DEMAND instances
(`purchasing_option` in `master-<host>.tf`), which ended the master-side
spot reclamations; only pg's legacy CFN master still rides a SpotFleet.
Worker-side spot interruptions are handled separately (`retry(agent())`
in the pipelines plus `disableTaskResubmit=true` on every arm fleet).
The four master-side mechanisms below were built for the spot era and
are retained as defense-in-depth for any abrupt instance loss:

1. **Capacity Rebalancing on the SpotFleet** — AWS launches a replacement
   on a rebalance recommendation (minutes to hours of advance warning),
   not just on the 2-minute interruption notice.
2. **Graceful spot-interrupt drain in the userdata** — a 30-second cron
   detects `spot/instance-action` IMDS metadata and runs
   `/usr/local/bin/jenkins-graceful-stop.sh`: quietDown, busyExecutors
   poll up to 85 s, safeExit, copy log, umount. `flock` prevents
   concurrent runs.
3. **MAX_SURVIVABILITY pipeline durability** — `init.groovy.d` sets the
   global default at every JVM start, so an abrupt JVM stop can be
   resumed at the same step.
4. **Hetzner worker rehydrate (PS-11173 Phase 4b)** — the Percona-patched
   Hetzner plugin re-adopts surviving Hetzner VMs as Jenkins agents on
   the next JVM start instead of letting `OrphanedNodesCleaner` reap
   them. `ControllerListener.onOnline` defers the cleaner by 5 minutes
   so the rehydrate path can claim VMs first. DC circuit-breaker state
   is also persisted to disk so a restart does not stampede a still-sick
   datacenter.

The readiness audit `scripts/check-master-spot-readiness.sh` walks every
moving piece (SpotFleet config, cron, graceful-stop.sh + flock, JVM args,
Secrets Manager + api-admin auth probe). Run it before declaring a master
ready to absorb an interrupt.

Full deep-dive (including the cases where workers are still lost on
reboot despite rehydrate) in [`ec2-master-resilience.md`](ec2-master-resilience.md).

## Detailed docs

- [`observability.md`](observability.md) — Mimir / Loki / Tempo / Grafana, push pipeline, cluster_label isolation
- [`karpenter.md`](karpenter.md) — NodePool tuning, spot fallback, taint exclusion
- [`pod-identity.md`](pod-identity.md) — IAM associations + agent addon
- [`argocd-bootstrap.md`](argocd-bootstrap.md) — GitOps Bridge + cluster-Secret annotations
- [`authentication.md`](authentication.md) — Duo SAML → Authentik → OIDC
- [`tls-strategy.md`](tls-strategy.md) — ACM wildcard + per-Ingress ssl-policy
- [`connectivity.md`](connectivity.md) — public path vs PrivateLink upgrade
- [`eks-hardening.md`](eks-hardening.md) — access entries, IMDSv2, KMS, log types
- [`jenkins-fleet-scrape.md`](jenkins-fleet-scrape.md) — per-master scrape via the push model

## Runbooks

- [`runbooks/bootstrap-state.md`](runbooks/bootstrap-state.md) — recreate the S3 state backend
- [`runbooks/eks-upgrade.md`](runbooks/eks-upgrade.md) — minor version bump procedure
- [`runbooks/mng-label-taint-changes.md`](runbooks/mng-label-taint-changes.md) — apply MNG label/taint edits without a drain
- [`runbooks/migrate-ps3-to-eks.md`](runbooks/migrate-ps3-to-eks.md) — cross-region EBS snapshot lift
- [`runbooks/decommission-ps3-ec2-master.md`](runbooks/decommission-ps3-ec2-master.md) — retire the ps3 EC2 spot master + re-parent its substrate
- [`runbooks/add-jenkins-host.md`](runbooks/add-jenkins-host.md) — bring a new Jenkins host onto the shared ALB
- [`runbooks/master-shell-access.md`](runbooks/master-shell-access.md) — shell on a master (`just ssh`, SSM, dynamic IPs, ps3 kubectl)
- [`runbooks/jenkins-ssl-cutover.md`](runbooks/jenkins-ssl-cutover.md) — per-master SSL cutover
- [`runbooks/disaster-recovery.md`](runbooks/disaster-recovery.md) — cluster recovery procedures
- [`runbooks/restore-mimir.md`](runbooks/restore-mimir.md) — Mimir block restore from S3
- [`runbooks/grafana-saml-cutover.md`](runbooks/grafana-saml-cutover.md) — Grafana SAML / Duo flip
- [`runbooks/authentik-bootstrap.md`](runbooks/authentik-bootstrap.md) — Authentik first-time configuration

## ADRs

Decision history lives in [`adr/`](adr/). Each architecture choice has a
one-page record.
