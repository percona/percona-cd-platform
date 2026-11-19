# 0050 - External Secrets store scoping

**Status:** Accepted for the namespace allowlist. The role per consumer is proposed.
**Related:** [ADR 0004](0004-pod-identity-default.md) (Pod Identity is the in-cluster AWS auth, `enable_irsa = false` in `terraform/eks.tf`)

## Context

- The External Secrets Operator reads AWS Secrets Manager through one Pod Identity role. That role may read every secret under `percona-ci-platform/*`.
- The one ClusterSecretStore `aws-secrets-manager` served every namespace. Whoever could create an ExternalSecret in any namespace could copy the Jenkins fleet tokens or the Authentik config into a Secret they can read.
- A namespaced SecretStore or AWS generator with no credential of its own runs on the same operator role. It bypasses any condition on a ClusterSecretStore.
- The chart aggregates create rights on ExternalSecrets and SecretStores into the built-in `edit` and `admin` roles.

## Decision

- **Namespace allowlist.**
  - `aws-secrets-manager` carries `spec.conditions[].namespaces` from `clusterSecretStore.allowedNamespaces` in the addon values. A new consumer namespace is added there in the same change as its first ExternalSecret. An empty list fails the render.
  - A ValidatingAdmissionPolicy refuses namespaced SecretStores with an AWS provider and the namespaced AWS generators (`denyNamespacedAwsStores`).
- Namespaced SecretStores with JWT service account auth are not used. They need an IAM OIDC provider and IRSA trust, which this cluster does not have.

## Consequences

- The allowlist limits who can use the store. It does not limit what an allowed namespace reads: any of the eight namespaces can still read every key under the prefix.
- A compromise of the operator pod still reaches every key.
- An ExternalSecret in a namespace outside the list fails with a store error until the namespace is added.

## Proposed next step: one role per consumer

- Each consumer gets an IAM role that reads only its own keys. The operator role assumes it through `spec.provider.aws.role` on a ClusterSecretStore of its own, which serves only the consumer's namespaces.
- That store sets `sessionTagsPolicy: Simple`, so the operator tags the session with the ExternalSecret's namespace. The role's trust requires that tag to name an allowed namespace.
- When a consumer's ExternalSecrets all use its own store, an explicit deny keeps its keys from the operator role. The shared store then cannot serve them.
- `authentik/config` is read by four namespaces and holds the bootstrap token next to the OIDC client secrets. Scoping ArgoCD, Grafana and Headlamp to their client secret needs that secret split first.
