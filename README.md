# percona-cd-platform

Percona's CI/CD platform.

It hosts Jenkins masters and the platform services around them — observability,
single sign-on, ingress with TLS termination, and cluster autoscaling — running
on a GitOps-managed EKS cluster in `us-east-1`. Everything is defined as code
and reconciled from this repo; there are no manual cluster changes.

Region, cluster name, hostnames, and other deployment-specific values are
parameterized in `terraform/`.

## Components

**CI/CD (Jenkins).** Jenkins masters served on `*.cd.percona.com`, in one of
two modes: an ALB → in-cluster NGINX proxy → cross-region VPC peering → EC2
master (a reconciler keeps each EndpointSlice synced to the live instance IP),
or an in-cluster StatefulSet. The custom master image bundles the WAR,
Percona-patched plugins, and `init.groovy.d`.

**Monitoring / observability.** A distributed LGTM stack (Mimir, Loki, Tempo,
Grafana). EC2 masters run a master-side Alloy that pushes metrics, logs, and
traces through `alloy-gateway` (NGINX bearer-auth + Alloy receivers) into the
stack. Grafana fronts all three behind Authentik OIDC.

**Terraform / AWS.** OpenTofu owns AWS-side state through "ArgoCD healthy":
VPC, EKS, managed node groups, Karpenter prerequisites, Pod Identity, ACM
wildcard cert, the LGTM S3 buckets, and each EC2 Jenkins master (SpotFleet,
IAM, EBS, userdata via a reusable module). TF outputs reach ArgoCD as
cluster-Secret annotations consumed as Helm values.

**GitOps / ArgoCD.** From "ArgoCD healthy" onward, everything in-cluster is
GitOps-managed. A root App-of-Apps fans out to ApplicationSets that reconcile
one Application per addon and one per in-cluster Jenkins instance. No manual
`kubectl` mutations — drift breaks reconciliation.

**Repo CI.** GitHub Actions runs lint + validate only — no plan, no deploy.
The same checks run locally via `just ci`.

## Quickstart

```sh
just ci                # local lint + validate
just tf-plan           # TF plan (writes tfplan)
just tf-apply          # TF apply (applies the saved tfplan; never auto-approve)
```

State bucket + lock are pre-created; see [the bootstrap runbook](docs/runbooks/bootstrap-state.md).

## Operating Terraform via the justfile

The justfile is the single entrypoint for Terraform. Drive every `tofu`
operation through a `just tf-*` recipe; do not run raw `tofu` or `cd terraform`
by hand.

- **`AWS_PROFILE` is required and supplied externally.** Export it in your shell
  (e.g. `export AWS_PROFILE=percona-dev-admin`); AWS-touching recipes fail loudly
  if it is unset. It is never baked into a default and never set in `terraform/`.
- **Back up state before any risky apply:** run `just tf-state-backup` (timestamped
  `tofu state pull`) first. `just tf-state-versioning-check` confirms bucket
  versioning is on.
- **`tf-plan` writes `tfplan`; `tf-apply` applies that saved plan** — never
  auto-approve. There is no `tf-apply-now`.
- **`-target` / `-exclude` are PLAN-ONLY.** `just tf-plan-masters` scopes a plan to
  the per-master modules for inspection; there is no `tf-apply-masters`. Targeting
  is for exceptional ops, not routine applies.

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

MNGs handle bootstrap and single-AZ stateful workloads whose PDBs block
eviction. Karpenter handles the higher-volume tiers (LGTM stateful,
stateless), trading multi-AZ HA for EBS-per-pod zonality. Full reasoning in
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

GNU Affero General Public License v3.0; see [`LICENSE`](LICENSE).
