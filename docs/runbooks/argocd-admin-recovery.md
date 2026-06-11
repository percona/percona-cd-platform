# ArgoCD admin recovery (break-glass)

When Authentik OIDC is unavailable (Authentik down, Duo outage, a broken
upgrade) and nobody can log in to ArgoCD. The local admin account is
disabled by design (`admin.enabled: "false"` in the chart values), so
recovery means re-enabling it temporarily.

Cluster access via `aws eks get-token` does not depend on Authentik, so
`kubectl` keeps working throughout. Get a kubeconfig with
`just kubeconfig` if needed.

## Re-enable the local admin

```bash
# 1. Turn the local admin on
kubectl -n argocd patch configmap argocd-cm \
  --type merge -p '{"data":{"admin.enabled":"true"}}'

# 2. Restart the server so it picks the flag up
kubectl -n argocd rollout restart deployment argocd-server

# 3. Clear any stale password so a fresh one generates
kubectl -n argocd patch secret argocd-secret \
  --type json -p '[{"op":"remove","path":"/data/admin.password"},
                   {"op":"remove","path":"/data/admin.passwordMtime"}]'

# 4. Restart again, the new admin password prints to server stdout
kubectl -n argocd rollout restart deployment argocd-server
kubectl -n argocd logs deployment/argocd-server | grep -i "admin password"
```

Log in as `admin` at `https://argo.cd.percona.com` (or port-forward with
`just argocd-ui` if the ALB path is also affected).

Note: `just argocd-password` reads `argocd-initial-admin-secret`, which has
been dead since the local admin was disabled after SSO validation. Use the
procedure above, not that recipe.

## Afterwards

Re-disable the account once SSO works again:

```bash
kubectl -n argocd patch configmap argocd-cm \
  --type merge -p '{"data":{"admin.enabled":"false"}}'
kubectl -n argocd rollout restart deployment argocd-server
```

The configmap patch is drift against the Terraform-rendered values. The
next `tofu apply` converges it back automatically, which is also why this
procedure never needs a revert PR.

## Related

- [`docs/argocd-bootstrap.md`](../argocd-bootstrap.md) for how the install
  and SSO wiring work.
- [`disaster-recovery.md`](disaster-recovery.md) for whole-cell recovery.
