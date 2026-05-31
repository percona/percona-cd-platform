# 0004 — EKS Pod Identity is the default; IRSA is the fallback

**Status:** Accepted (2026-04-30)

## Context

Two ways to grant AWS IAM permissions to a Kubernetes ServiceAccount on EKS:

1. **IRSA** (since 2019) — OIDC trust between cluster and IAM, role assumed via
   the SDK's web-identity provider.
2. **Pod Identity** (GA Nov 2023) — a managed agent runs as a DaemonSet, vends
   credentials to pods over a pinned link-local endpoint.

Pod Identity is now the AWS-recommended default; it removes per-cluster OIDC
trust policy churn and supports cross-account associations cleanly.

## Decision

Use **EKS Pod Identity** for all five day-one addons:

- AWS Load Balancer Controller
- external-dns
- Karpenter
- AWS EBS CSI Driver
- External Secrets Operator

Wired via `terraform-aws-modules/eks-pod-identity/aws` v2.8.0 with
`attach_<addon>_policy = true`. The `eks-pod-identity-agent` managed addon is
mandatory — without it, every association silently no-ops.

IRSA stays available via `terraform-aws-modules/iam/aws//modules/iam-role-for-
service-accounts-eks` for edge cases (legacy SDK quirks).

**Update 2026-05-31 (post-review):** IRSA was subsequently *retired*, not merely kept as a fallback. `terraform/eks.tf` sets `enable_irsa = false` and the cluster's IAM OIDC provider was deleted, so the `iam_irsa` pin in `versions.tf` is now dead code (instantiated nowhere) and should be dropped. Every in-cluster consumer is on Pod Identity. The ps3-k8s verification gate below was also mooted: the masters are EKS-*fronted* EC2 ([ADR 0019](0019-shared-alb-ssl-termination-for-jenkins-masters.md)), not in-cluster pods, and the EC2-plugin credential problem was solved by the patched plugin fork + `e-ec2-irsa-credential.groovy`, not by Pod Identity (see `docs/lessons-from-poc.md`).

## Consequences

- Adding a new addon: one `local.modules.pod_identity` block in
  `terraform/pod-identity.tf` instead of an IRSA role + OIDC trust patch.
- The Jenkins EC2 plugin's classloader-isolated SDK v1 has historically
  intercepted IRSA's web-identity provider; behaviour under Pod Identity is
  **unverified** and must be tested on `ps3-k8s` (the first in-cluster
  master) before committing the rest of the fleet's lift to it. See
  `docs/lessons-from-poc.md`.
