# percona-cd-platform

Percona's CI/CD platform: a GitOps-managed EKS cluster in `us-east-1` hosting
the Jenkins masters and the platform services around them (LGTM observability,
Authentik SSO, ingress with TLS, autoscaling). Everything is defined as code
and reconciled from this repo. There are no manual cluster changes.

## Key facts

- OpenTofu owns AWS up to "ArgoCD healthy": VPC, EKS, node groups, Pod
  Identity, the EC2 Jenkins masters, ARM spot fleets, S3, cleanup reapers.
  TF outputs reach ArgoCD as cluster-Secret annotations.
- From there ArgoCD owns everything in-cluster: a root App-of-Apps fans out
  ApplicationSets that reconcile one Application per `resources/addons/*` dir
  and one per `resources/jenkins/master/instances/*` dir. No manual `kubectl`.
- Jenkins masters serve on `*.cd.percona.com` in two modes: EKS-fronted EC2
  (ALB, in-cluster NGINX, cross-region VPC peering, an EndpointSlice
  reconciler) or in-cluster StatefulSet. Hostnames resolve to the ALB
  (HTTPS only). A shell goes through SSM
  ([runbook](docs/runbooks/master-shell-access.md)).
- Repo CI is lint + validate only. `ci-gate` is the single required check and
  `just ci` mirrors it locally.
- The repo is public: no account IDs, ARNs, or secrets in committed files.

## Layout

| Path | What lives there |
|---|---|
| [`terraform/`](terraform/) | AWS substrate. Conventions in [`terraform/CLAUDE.md`](terraform/CLAUDE.md) (file-naming grammar, per-team `# Owner:` banners, tags). Reusable modules carry their own READMEs ([jenkins-arm-fleet](terraform/modules/jenkins-arm-fleet/README.md), [jenkins-arm-standalone](terraform/modules/jenkins-arm-standalone/README.md), [scheduled-lambda](terraform/modules/scheduled-lambda/README.md)). Pins in [`versions.tf`](terraform/versions.tf) |
| [`argocd-bootstrap/`](argocd-bootstrap/) | Root Application, ApplicationSets, AppProject |
| [`resources/addons/`](resources/addons/) | One dir = one ArgoCD Application (observability, ingress, SSO, ...) |
| [`resources/jenkins/`](resources/jenkins/) | In-cluster master chart, per-instance values, clouds catalog (rendered by [`scripts/render-clouds.py`](scripts/render-clouds.py), drift-gated in CI) |
| [`images/`](images/) | Container images (controller bundle and friends), built by GitHub Actions |
| [`scripts/`](scripts/) | Verification and render tooling. Catalog in [`scripts/README.md`](scripts/README.md) |
| [`docs/`](docs/) | [Architecture](docs/architecture.md), [ADRs](docs/adr/), [runbooks](docs/runbooks/). Everything is indexed in [`docs/README.md`](docs/README.md) |
| [`justfile`](justfile) | The single entrypoint for CI and every `tofu` operation |

## Quickstart

```sh
just ci                # local lint + validate (mirrors the PR gate)
just tf-plan           # TF plan (writes tfplan)
just tf-apply          # apply the saved tfplan, never auto-approve
just ssh               # list the running Jenkins masters (just ssh <inst> opens a shell)
```

`AWS_PROFILE` must be exported in your shell. AWS-touching recipes fail loudly
without it. Back up state before risky applies (`just tf-state-backup`). State
bucket bootstrap: [runbook](docs/runbooks/bootstrap-state.md).

### Tool requirements

| Tool | Used for |
|---|---|
| [`just`](https://github.com/casey/just) | The single entrypoint. Every workflow below is a recipe |
| [OpenTofu](https://opentofu.org/) (`tofu`) | All terraform operations (version pin at the top of the [`justfile`](justfile)) |
| AWS CLI v2 | Every AWS-touching recipe. SSO login via `aws sso login` |
| [session-manager-plugin](https://docs.aws.amazon.com/systems-manager/latest/userguide/session-manager-working-with-install-plugin.html) | Interactive `just ssh <inst>` sessions (one-shot `just ssm-run` works without it) |
| `kubectl` | Cluster access (`just kubeconfig`), ps3 shell |
| [`uv`](https://github.com/astral-sh/uv) | Python script gates and lambda tests inside `just ci` |
| Docker (buildx) | `just build-image` only |
| trivy, yamllint, actionlint, zizmor, kubeconform | The `just ci` lint set. Version pins sit at the top of the justfile (helm is fetched and sha-verified automatically) |

## Where the details are

| Topic | Doc |
|---|---|
| System architecture and components | [`docs/architecture.md`](docs/architecture.md) |
| Compute tiers, MNG vs Karpenter reasoning | [ADR 0017](docs/adr/0017-cluster-tier-taxonomy-and-lgtm-pinning.md) |
| Observability push pipeline | [`docs/observability.md`](docs/observability.md) |
| EC2 master connectivity and resilience | [`docs/connectivity.md`](docs/connectivity.md), [`docs/ec2-master-resilience.md`](docs/ec2-master-resilience.md) |
| Shell access to the masters (`just ssh`, SSM) | [`docs/runbooks/master-shell-access.md`](docs/runbooks/master-shell-access.md) |
| Account cleanup reapers | [`docs/runbooks/cleanup-reapers.md`](docs/runbooks/cleanup-reapers.md) |
| Bootstrap, recovery, upgrades | [`docs/runbooks/`](docs/runbooks/) |
| Token-free Jenkins MCP gateway (connect an MCP client) | [`images/jenkins-mcp/README.md`](images/jenkins-mcp/README.md) |
| Every past design decision | [`docs/adr/`](docs/adr/) |

## Contributing

- `just ci` must pass before PR. Pre-commit hooks approximate it ([`.pre-commit-config.yaml`](.pre-commit-config.yaml) runs `terraform_fmt` plus a local `tofu-validate` hook; the stock `terraform_validate` hook is intentionally omitted because it shells the `terraform` binary, not tofu). `just tf-validate` is the real gate.
- Terraform changes follow [`terraform/CLAUDE.md`](terraform/CLAUDE.md), gated fail-closed by [`scripts/check_conventions.py`](scripts/check_conventions.py) (part of `just ci`).
- Propose architecture changes in [`docs/adr/`](docs/adr/) first.
- Version pins live in [`terraform/versions.tf`](terraform/versions.tf). Run [`scripts/check_versions.py`](scripts/check_versions.py) before bumping.
- Commit format: `type(scope): subject`. No AI footers.

## License

GNU Affero General Public License v3.0, see [`LICENSE`](LICENSE).
