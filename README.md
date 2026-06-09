# percona-cd-platform

Percona's CI/CD platform: a GitOps-managed EKS cluster in `us-east-1` hosting
the Jenkins masters and the platform services around them (LGTM observability,
Authentik SSO, ingress with TLS, autoscaling). Everything is defined as code
and reconciled from this repo; there are no manual cluster changes.

## Key facts

- OpenTofu owns AWS up to "ArgoCD healthy": VPC, EKS, node groups, Pod
  Identity, the EC2 Jenkins masters, ARM spot fleets, S3, cleanup reapers.
  TF outputs reach ArgoCD as cluster-Secret annotations.
- From there ArgoCD owns everything in-cluster: a root App-of-Apps fans out
  ApplicationSets that reconcile one Application per `resources/addons/*` dir
  and one per `resources/jenkins/master/instances/*` dir. No manual `kubectl`.
- Jenkins masters serve on `*.cd.percona.com` in two modes: EKS-fronted EC2
  (ALB, in-cluster NGINX, cross-region VPC peering, an EndpointSlice
  reconciler) or in-cluster StatefulSet.
- Repo CI is lint + validate only; `ci-gate` is the single required check and
  `just ci` mirrors it locally.
- The repo is public: no account IDs, ARNs, or secrets in committed files.

## Layout

| Path | What lives there |
|---|---|
| [`terraform/`](terraform/) | AWS substrate; reusable modules with their own READMEs ([jenkins-arm-fleet](terraform/modules/jenkins-arm-fleet/README.md), [jenkins-arm-standalone](terraform/modules/jenkins-arm-standalone/README.md), [scheduled-lambda](terraform/modules/scheduled-lambda/README.md)); pins in [`versions.tf`](terraform/versions.tf) |
| [`argocd-bootstrap/`](argocd-bootstrap/) | Root Application, ApplicationSets, AppProject |
| [`resources/addons/`](resources/addons/) | One dir = one ArgoCD Application (observability, ingress, SSO, ...) |
| [`resources/jenkins/`](resources/jenkins/) | In-cluster master chart, per-instance values, clouds catalog (rendered by [`scripts/render-clouds.py`](scripts/render-clouds.py), drift-gated in CI) |
| [`images/`](images/) | Container images (controller bundle and friends), built by GitHub Actions |
| [`scripts/`](scripts/) | Verification and render tooling; catalog in [`scripts/README.md`](scripts/README.md) |
| [`docs/`](docs/) | [Architecture](docs/architecture.md), [ADRs](docs/adr/), [runbooks](docs/runbooks/); everything indexed in [`docs/README.md`](docs/README.md) |
| [`justfile`](justfile) | The single entrypoint for CI and every `tofu` operation |

## Quickstart

```sh
just ci                # local lint + validate (mirrors the PR gate)
just tf-plan           # TF plan (writes tfplan)
just tf-apply          # apply the saved tfplan; never auto-approve
```

`AWS_PROFILE` must be exported in your shell; AWS-touching recipes fail loudly
without it. Back up state before risky applies (`just tf-state-backup`). State
bucket bootstrap: [runbook](docs/runbooks/bootstrap-state.md).

## Where the details are

| Topic | Doc |
|---|---|
| System architecture and components | [`docs/architecture.md`](docs/architecture.md) |
| Compute tiers, MNG vs Karpenter reasoning | [ADR 0017](docs/adr/0017-cluster-tier-taxonomy-and-lgtm-pinning.md) |
| Observability push pipeline | [`docs/observability.md`](docs/observability.md) |
| EC2 master connectivity and resilience | [`docs/connectivity.md`](docs/connectivity.md), [`docs/ec2-master-resilience.md`](docs/ec2-master-resilience.md) |
| Account cleanup reapers | [`docs/runbooks/cleanup-reapers.md`](docs/runbooks/cleanup-reapers.md) |
| Bootstrap, recovery, upgrades | [`docs/runbooks/`](docs/runbooks/) |
| Every past design decision | [`docs/adr/`](docs/adr/) |

## Contributing

- `just ci` must pass before PR; pre-commit hooks mirror it ([`.pre-commit-config.yaml`](.pre-commit-config.yaml)).
- Propose architecture changes in [`docs/adr/`](docs/adr/) first.
- Version pins live in [`terraform/versions.tf`](terraform/versions.tf); run [`scripts/check_versions.py`](scripts/check_versions.py) before bumping.
- Commit format: `type(scope): subject`. No AI footers.

## License

GNU Affero General Public License v3.0; see [`LICENSE`](LICENSE).
