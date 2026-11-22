# 0050 - External Secrets store scoping

**Status:** Accepted for the namespace allowlist. The role per consumer is built and switched off.
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

## Role per consumer (in use for Mimir)

- `terraform/iam-external-secrets-stores.tf` creates one IAM role per entry of `local.eso_store_consumers`. The role may read only the consumer's keys, by exact name with the 6 character suffix of Secrets Manager. The first consumer is `jenkins-mcp`.
- The role trusts only the operator's Pod Identity role, for `sts:AssumeRole` and `sts:TagSession`, and only when the session tag `esoNamespace` names one of the consumer's namespaces. `terraform/pod-identity.tf` lets the operator role assume these roles.
- A `scopedStores` entry in the addon values renders a ClusterSecretStore `aws-secrets-manager-<consumer>`. It sets `spec.provider.aws.role` to the consumer's role, `sessionTagsPolicy: Simple` and a namespace condition. The operator then tags each session with the namespace of the ExternalSecret.
- `exclusive = true` on a consumer adds an explicit deny for its keys to the operator role. The shared store then cannot serve them.
- Mimir is the first consumer with a scoped store (`scopedStores.mimir`, ADR 0051). It was never on the shared store's namespace list. `jenkins-mcp` has its role and still uses the shared store. `exclusive` is `false` for both.
- `authentik/config` is read by four namespaces and holds the bootstrap token next to the OIDC client secrets. Scoping ArgoCD, Grafana and Headlamp to their client secret needs that secret split first.

### Switch on one consumer

1. Apply the Terraform. Read the role name from the output `eso_store_role_names`.
2. Add the consumer to `scopedStores` with `roleName` and `namespaces`.
3. Set the consumer chart's store to `aws-secrets-manager-<consumer>` (`externalSecret.storeName` for jenkins-mcp).
4. Check that every ExternalSecret of the consumer shows `SecretSynced`.
5. Set `exclusive = true` for the consumer and apply the Terraform.
