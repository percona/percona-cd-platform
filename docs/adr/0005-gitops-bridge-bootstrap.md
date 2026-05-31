# 0005 — GitOps Bridge bootstrap pattern

**Status:** Accepted (2026-04-30)

## Context

Need a clean handoff between Terraform (owns AWS-side state) and ArgoCD (owns
in-cluster state). Naively: pass IAM role ARNs, ACM ARNs, OIDC URLs into
addon Helm values via Terraform-templated YAML committed to git — couples the
GitOps repo to a specific account/region/cluster.

## Decision

Adopt the **GitOps Bridge** pattern — hand-rolled with `helm_release` + `kubernetes_secret_v1` + `kubectl_manifest`, **not** the `gitops-bridge-dev/gitops-bridge` module (that module is referenced nowhere in `terraform/`; only the pattern was adopted):

1. Terraform installs the `argo-cd` Helm chart (v9.5.9) in HA after the EKS
   control plane and Pod Identity associations are healthy.
2. Terraform writes a `Secret` labelled `argocd.argoproj.io/secret-type:
   cluster` whose **annotations** carry every TF output ArgoCD needs:
   `cluster_name`, `aws_account_id`, `aws_region`, `oidc_provider_arn`,
   `acm_wildcard_arn`, `karpenter_sqs_name`, `karpenter_node_role_arn`, role
   ARNs for every Pod Identity association.
3. Terraform applies a single root `Application` pointing at
   `argocd-bootstrap/`.
4. ApplicationSets read the cluster-secret annotations as Helm `valuesObject`,
   so addon `values.yaml` files contain zero account/region/ARN data.

## Consequences

- Addon values files stay environment-agnostic — same git ref applies to
  staging, prod, future regional clusters.
- Adding a new TF output + ArgoCD-consumed value is two edits: write the
  annotation in `terraform/argocd.tf`, read it via `valuesObject` in the
  ApplicationSet.
- The ArgoCD chart upgrade path is owned by Terraform on day one. Once steady-
  state is reached we may move it into the addons ApplicationSet (self-managed
  ArgoCD) — that is a v1.5+ decision.
