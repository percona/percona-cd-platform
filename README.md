# percona-ci-platform

Public OpenTofu + ArgoCD platform repo. Provisions an EKS cluster (`us-east-1`,
EKS 1.35) and bootstraps the addon stack via GitOps. Hosts Percona's CI
infrastructure: Jenkins masters, observability (LGTM), shared ALB SSL
termination for `*.cd.percona.com`.

Migrating from `nogueiraanderson/` to `Percona-Lab/` once stable.

## Repo layout

| Path | Owner | What |
|---|---|---|
| `terraform/` | OpenTofu | VPC, EKS, addons, Pod Identity, ACM, ArgoCD bootstrap |
| `argocd-bootstrap/` | ArgoCD | Root App-of-Apps + ApplicationSets |
| `resources/addons/` | ArgoCD | Helm umbrella charts per addon |
| `image/` | Docker build | Jenkins 2.x + plugins |
| `docs/` | Markdown | Architecture, runbooks, ADRs, lessons |
| `.github/workflows/` | CI | Lint + validate (no plan, no deploy) |

## Quickstart

```sh
just ci                # local lint + validate
just tf-plan           # TF plan
just tf-apply          # TF apply
```

State bucket + lock are pre-created — see [`docs/runbooks/bootstrap-state.md`](docs/runbooks/bootstrap-state.md).

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
