# tests/integration

Live, on-demand audits that exercise the real account and cluster. They are
**not** part of the PR gate (CI has no AWS credentials or kube context); the
PR-gated, no-AWS regression that keeps the secret fences in Terraform is
`terraform/lambdas/tests/test_secret_resource_policies.py`.

Each file is a self-contained PEP-723 uv script. Run via `just`:

| Recipe | What it checks | Needs |
|--------|----------------|-------|
| `just audit-secret-access` | Who can `GetSecretValue` on the cluster's Secrets Manager secrets directly from AWS; asserts the deny-by-default fences. | `AWS_PROFILE`, read-only |
| `just audit-argocd-rbac` | The `percona` group (ArgoCD `role:readonly`) cannot read secret values. | `argocd` CLI + kube context |

Both are read-only and print no secret values. They skip cleanly when the
required tooling or credentials are absent.

Honesty-by-construction note: `test_eso_secret_access_audit.py` tracks the
`grafana/admin` gap as a strict-xfail. After `secrets-policies.tf` is applied
and that secret is fenced, the xfail flips to a failure on purpose, forcing the
secret to be reclassified from `gap` to `fenced` in the same change.
