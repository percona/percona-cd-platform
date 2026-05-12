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
        StatefulSet jenkins-<host>      Deployment jenkins-proxy-<host>
        (NodePool jenkins-system,                  │
         AZ us-east-1a, gp3 PVC)                   ▼
                                          origin-<host>.cd.percona.com
                                                   │
                                                   ▼
                                          EC2 Jenkins master (other region)
```

## Day-one host scope

| Host(s) | Role | Why |
|---|---|---|
| `ps3-k8s.cd.percona.com` | Mode A (in-cluster StatefulSet) | First in-cluster Jenkins master. Seeded as a full replica of the production EC2 ps3 (cross-region EBS snapshot copy of `JENKINS_HOME`). See `runbooks/migrate-ps3-to-eks.md`. |
| `pmm`, `ps80`, `pxc`, `pxb`, `psmdb`, `pg`, `ps57`, `rel`, `cloud` (`.cd.percona.com`) | Mode B (ALB → in-cluster NGINX → EC2 origin) | Friendly DNS flips to ALB, traffic still ends up at the existing EC2 master via `origin-<host>.cd.percona.com`. |
| `ps3.cd.percona.com` | **Untouched** — stays on its current direct EC2 path | Production traffic keeps flowing during validation of `ps3-k8s`. Cutover happens later as a Route 53 flip; until then, ps3 is intentionally outside this platform's `var.jenkins_hosts` map. |
| `grafana.cd.percona.com`, `argocd.cd.percona.com` | In-cluster Service behind the same ALB | Platform-managed UIs. |

## Ownership boundary

- **Terraform / OpenTofu** owns AWS-side state up to "ArgoCD healthy."
- **ArgoCD** owns everything in-cluster from there. App-of-Apps + ApplicationSets reconcile from `resources/`.
- **The cluster Secret** (`argocd.argoproj.io/secret-type: cluster`) carries TF outputs (cluster name, OIDC, role ARNs, ACM ARN, Karpenter SQS) as annotations. ApplicationSets read those annotations as Helm `valuesObject`.

## NodeGroups

Tier taxonomy from [ADR 0017](adr/0017-cluster-tier-taxonomy-and-lgtm-pinning.md). Every node carries `workload.percona.com/tier=<tier>`; consumers select via that key.

| NG | Capacity | AZ | Tier label | Taint | Hosts |
|---|---|---|---|---|---|
| `system` | on-demand (`m6a.large × 3`) | multi-AZ | `bootstrap` | `CriticalAddonsOnly=true:NoSchedule` | bootstrap-tier addons (Karpenter controller, ArgoCD, AWS LB Controller, external-dns, external-secrets, kube-state-metrics) -- explicit tolerations required |
| `prometheus_system` | on-demand (`m6a.large × 1`) | us-east-1a | `obs-state` | `workload.percona.com/tier=obs-state:NoSchedule` | LGTM stateful pods (Mimir ingester/store-gateway, Loki ingester, Tempo ingester, Grafana) -- single-AZ for EBS zonality |
| `jenkins_system` | on-demand (`m6a.xlarge × N`) | us-east-1a | `jenkins-master` | `workload.percona.com/tier=jenkins-master:NoSchedule` | Jenkins master StatefulSets (`ps3-k8s` today, more after migration) |
| Karpenter NodePool `default` | spot + on-demand fallback | multi-AZ | (Karpenter-provisioned) | `NotIn` the two stateful tier taints | Everything else: jenkins-proxy NGINX Deployments, Alloy agents, ArgoCD ApplicationSet workloads |

`m6a.large × 3` for `system` replaced the original `t3.medium × 2` after a 2026-05-11 control-plane outage: kubelet timed out responding to API-server health checks on t3.medium nodes when CPUCreditBalance hit zero. m6a.large is non-burstable (fixed price, no credit dynamics). See `terraform/locals.tf` `ng.system` comment for the full incident note.

## Storage

| StorageClass | Default | AZ | Reclaim | Used by |
|---|---|---|---|---|
| `gp3` | yes | multi-AZ | Delete | Generic workloads |
| `gp3-monitoring-1a-retain` | no | us-east-1a | Retain | Prometheus, Alertmanager, Grafana |
| `gp3-jenkins-1a-retain` | no | us-east-1a | Retain | Jenkins master JENKINS_HOME PVCs |

## Sync waves (ArgoCD)

| Wave | Addon | Reason |
|---|---|---|
| 0 | `storageclass-gp3` | Required before any PVC binds |
| 0 | `external-secrets` | Token + secret sync for everything downstream |
| 1 | `aws-load-balancer-controller` | Ingresses need it |
| 2 | `external-dns` | Needs LB Controller to publish ALB endpoints |
| 3 | `karpenter` | After LB controller |
| 4 | LGTM stack (mimir, loki, tempo, grafana, alloy + prometheus-operator-crds, kube-state-metrics, prometheus-node-exporter) | Last -- everything they scrape / receive pushes from is up. See [ADR 0016](adr/0016-lgtm-only-metrics-stack.md) for the kube-prometheus-stack retirement. |

`cert-manager` is intentionally not in v1 — see `docs/adr/0007-cert-manager-deferred.md`.

## Detailed docs

- [`connectivity.md`](connectivity.md) — public path vs PrivateLink upgrade
- [`tls-strategy.md`](tls-strategy.md) — ACM wildcard + per-Ingress ssl-policy
- [`pod-identity.md`](pod-identity.md) — five associations + agent addon
- [`argocd-bootstrap.md`](argocd-bootstrap.md) — GitOps Bridge mechanics
- [`karpenter.md`](karpenter.md) — NodePool tuning, spot fallback, tier-taint exclusion
- [`observability.md`](observability.md) — LGTM stack values, AZ pinning, master-side Alloy push pipeline (ADR 0013)
- [`jenkins-fleet-scrape.md`](jenkins-fleet-scrape.md) — Probe / additionalScrapeConfigs / bearer-token / Option A→B migration
- [ADR 0010](adr/0010-distributed-lgtm.md), [ADR 0016](adr/0016-lgtm-only-metrics-stack.md) — distributed LGTM stack, kube-prometheus-stack retirement
- [`lessons-from-poc.md`](lessons-from-poc.md) — verbatim lift from the prior PoC

## Runbooks

- [`runbooks/bootstrap-state.md`](runbooks/bootstrap-state.md) — recreate the S3 state backend from scratch
- [`runbooks/eks-upgrade.md`](runbooks/eks-upgrade.md) — minor version bump procedure
- [`runbooks/migrate-ps3-to-eks.md`](runbooks/migrate-ps3-to-eks.md) — cross-region EBS snapshot lift
- [`runbooks/restore-mimir.md`](runbooks/restore-mimir.md) — Mimir block restore from S3 versioning
- [`runbooks/grafana-saml-cutover.md`](runbooks/grafana-saml-cutover.md) — flip SAML/Duo on once HD-30780 closes

## ADRs

Decision history lives in [`adr/`](adr/). Each architecture choice has a one-page record.
