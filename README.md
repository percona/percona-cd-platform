# percona-ci-platform

Public OpenTofu + ArgoCD platform repo. Provisions an EKS cluster (`us-east-1`,
EKS 1.35) and bootstraps the addon stack via GitOps. Hosts Percona's CI
infrastructure: Jenkins masters, observability (LGTM), shared ALB SSL
termination for `*.cd.percona.com`.

Migrating from `nogueiraanderson/` to `Percona-Lab/` once stable.

## Repo layout

| Path | Owner | What |
|---|---|---|
| `terraform/` | OpenTofu | VPC, EKS, addons, Pod Identity, ACM, ArgoCD bootstrap, per-master `origin-<host>` Route53 records (`origins.tf`) |
| `argocd-bootstrap/` | ArgoCD | Root App-of-Apps + ApplicationSets |
| `resources/addons/` | ArgoCD | Helm umbrella charts per addon (includes `jenkins-ingress`, the shared-ALB SSL termination for EC2 Jenkins masters, see [ADR 0019](docs/adr/0019-shared-alb-ssl-termination-for-jenkins-masters.md)) |
| `image/` | Docker build | Jenkins 2.x + plugins |
| `docs/` | Markdown | Architecture, runbooks (including [`jenkins-ssl-cutover.md`](docs/runbooks/jenkins-ssl-cutover.md) for the per-master cutover), ADRs, lessons |
| `.github/workflows/` | CI | Lint + validate (no plan, no deploy) |

## Quickstart

```sh
just ci                # local lint + validate
just tf-plan           # TF plan
just tf-apply          # TF apply
```

State bucket + lock are pre-created, see [`docs/runbooks/bootstrap-state.md`](docs/runbooks/bootstrap-state.md).

## Compute topology

Five tiers, each with a single canonical `workload.percona.com/tier` label and
(where exclusive) a matching taint. Workloads opt in via `nodeSelector` +
`tolerations`. `general` is untainted and is the safe fallthrough.

| Tier | Capacity | Notes |
|---|---|---|
| `bootstrap` | EKS MNG, `m6a.large` × 3 multi-AZ on-demand | ArgoCD, Karpenter itself, AWS LB controller, external-secrets. Taint `CriticalAddonsOnly=true:NoSchedule` (AWS-canonical key the chart defaults already tolerate). |
| `lgtm-stateful` | Karpenter NodePool, on-demand `r7a/r7i/m7a/m7i × large/xlarge/2xlarge` multi-AZ | Mimir/Loki/Tempo ingesters, store-gateway, compactor, alertmanager. Configured to behave like an MNG (no spot, no consolidation under load, no AMI-drift recycling, every pod carries `karpenter.sh/do-not-disrupt`) while keeping multi-AZ and instance-family flex. |
| `obs-state` | EKS MNG `prometheus_system`, `m6a.large` × 1 us-east-1a | kube-state-metrics, prometheus-operator-CRDs, single-AZ supports. |
| `jenkins-master` | EKS MNG `jenkins_system`, `m6a.xlarge` × 1 us-east-1a | In-cluster Jenkins master PoC (`jenkins-ps3-k8s`). |
| `general` | Karpenter NodePool `default`, spot + on-demand `c7/m7/r7-i/a` | Stateless LGTM components, Grafana, alloy-gateway, Authentik web tier, anything without an explicit tier. |

Both EKS MNGs and Karpenter NodePools are in use. MNGs handle bootstrap and
the single-AZ pinned stateful workloads; Karpenter covers everything that
benefits from multi-AZ + instance-family flex (LGTM stateful, all stateless).
The 2026-05-11 LGTM outage drove the stateful split, see
[`docs/adr/0017-cluster-tier-taxonomy-and-lgtm-pinning.md`](docs/adr/0017-cluster-tier-taxonomy-and-lgtm-pinning.md)
for the full reasoning.

## Documentation

| Topic | Doc |
|---|---|
| Architecture overview | [`docs/architecture.md`](docs/architecture.md) |
| Authentication (Duo SAML → Authentik → OIDC) | [`docs/authentication.md`](docs/authentication.md) |
| Red-team review (2026-05-07) | [`docs/security-review-2026-05-07.md`](docs/security-review-2026-05-07.md) |
| ArgoCD bootstrap | [`docs/argocd-bootstrap.md`](docs/argocd-bootstrap.md) |
| EKS hardening | [`docs/eks-hardening.md`](docs/eks-hardening.md) |
| Pod Identity (vs IRSA) | [`docs/pod-identity.md`](docs/pod-identity.md) |
| Karpenter | [`docs/karpenter.md`](docs/karpenter.md) |
| Observability (LGTM) | [`docs/observability.md`](docs/observability.md) |
| TLS strategy | [`docs/tls-strategy.md`](docs/tls-strategy.md) |
| Connectivity | [`docs/connectivity.md`](docs/connectivity.md) |
| Jenkins fleet scrape | [`docs/jenkins-fleet-scrape.md`](docs/jenkins-fleet-scrape.md) |
| Runbooks | [`docs/runbooks/`](docs/runbooks/) |
| ADRs | [`docs/adr/`](docs/adr/) |
| PoC history & lessons | [`docs/poc-history.md`](docs/poc-history.md), [`docs/lessons-from-poc.md`](docs/lessons-from-poc.md) |

Pinned versions: [`terraform/versions.tf`](terraform/versions.tf). Run
[`scripts/check_versions.py`](scripts/check_versions.py) before changing pins.

## Contributing

- `just ci` must pass before PR.
- Pre-commit hooks mirror CI ([`.pre-commit-config.yaml`](.pre-commit-config.yaml)).
- ADRs in [`docs/adr/`](docs/adr/) — propose architecture changes there first.
- Commit format: `type(scope): subject`. No AI footers.

## License

Apache-2.0.
