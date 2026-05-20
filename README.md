# percona-cd-platform

Public OpenTofu + ArgoCD platform repo.

Provisions an AWS EKS cluster and bootstraps its addon stack via GitOps.
Hosts a self-service CI/CD environment: Jenkins masters, an observability
stack (Grafana + Mimir + Loki + Tempo), and a shared ALB for SSL termination
across the hosted services.

Region, cluster name, hostnames, and other deployment-specific values are
parameterized in `terraform/`.

## Repo layout

| Path | Owner | What |
|---|---|---|
| `terraform/` | OpenTofu | VPC, EKS, addons, Pod Identity, ACM, ArgoCD bootstrap, per-master DNS records |
| `argocd-bootstrap/` | ArgoCD | Root App-of-Apps + ApplicationSets |
| `resources/` | ArgoCD | Helm-based workloads. `addons/` is one umbrella per cluster addon (observability stack, ingress, identity, Karpenter, etc.); `jenkins/` is the in-cluster Jenkins master chart with per-instance overlays. |
| `image/` | Docker | Jenkins image (war + plugins) |
| `docs/` | Markdown | Architecture, runbooks, ADRs |
| `scripts/` | Bash + Python | Operational helpers; see [`scripts/README.md`](scripts/README.md) |
| `.github/workflows/` | CI | Lint + validate (no plan, no deploy) |

## Quickstart

```sh
just ci                # local lint + validate
just tf-plan           # TF plan
just tf-apply          # TF apply
```

State bucket + lock are pre-created; see [the bootstrap runbook](docs/runbooks/bootstrap-state.md).

## Compute topology

Four tiers, each with a canonical `workload.percona.com/tier` label and
(where exclusive) a matching taint. Workloads opt in via `nodeSelector` +
`tolerations`. `general` is untainted and is the safe fallthrough.

| Tier | Capacity | Hosts |
|---|---|---|
| `bootstrap` | EKS MNG, on-demand, multi-AZ | ArgoCD, Karpenter, AWS LB controller, external-secrets, external-dns, kube-state-metrics |
| `obs-state` | EKS MNG, single-AZ | Stateful single-replica pods that block eviction (Authentik Postgres, Grafana, prometheus-operator CRDs) |
| `lgtm-stateful` | Karpenter NodePool, on-demand, single-AZ | Stateful LGTM pods (Mimir, Loki, Tempo ingesters; store-gateway; compactor; alertmanager). Configured to behave like an MNG (no spot, no consolidation under load, no AMI-drift) while keeping instance-family flex |
| `general` | Karpenter NodePool, spot + on-demand, single-AZ | Stateless LGTM components, Grafana web, the auth web tier, alloy-gateway, anything without an explicit tier |

MNGs handle bootstrap and single-AZ stateful workloads with PDBs that
block eviction. Karpenter handles the higher-volume tiers (LGTM stateful,
stateless), trading off multi-AZ HA for EBS-per-pod zonality.

The stateful split was driven by a real outage; full reasoning in
[the cluster tier taxonomy ADR](docs/adr/0017-cluster-tier-taxonomy-and-lgtm-pinning.md).

## Documentation

| Topic | Doc |
|---|---|
| Architecture overview | [`docs/architecture.md`](docs/architecture.md) |
| Architecture Decision Records | [`docs/adr/`](docs/adr/) |
| Runbooks (bootstrap, recovery, upgrades) | [`docs/runbooks/`](docs/runbooks/) |

Everything else is indexed in [`docs/README.md`](docs/README.md).

## Contributing

- `just ci` must pass before PR.
- Pre-commit hooks mirror CI ([`.pre-commit-config.yaml`](.pre-commit-config.yaml)).
- Propose architecture changes in [`docs/adr/`](docs/adr/) first.
- Pinned versions live in [`terraform/versions.tf`](terraform/versions.tf); run [`scripts/check_versions.py`](scripts/check_versions.py) before bumping pins.
- Commit format: `type(scope): subject`. No AI footers.

## License

GNU Affero General Public License v3.0; see [`LICENSE`](LICENSE). This
matches [`Percona-Lab/jenkins-pipelines`](https://github.com/Percona-Lab/jenkins-pipelines),
which this repo consumes (master-side install script fetched in userdata).
