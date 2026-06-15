# 0003 — ACM wildcard for the ALB, cert-manager deferred

**Status:** Accepted (2026-04-30)

## Context

The shared ALB needs a TLS certificate covering all 10 Jenkins hostnames plus
`grafana.cd.percona.com`, `argocd.cd.percona.com`, and (until #228 retired them)
the per-master `origin-<host>.cd.percona.com` names then used by the
reverse-proxy data path.

Two options: AWS Certificate Manager (ACM) or in-cluster cert-manager with a
public ACME issuer (Let's Encrypt).

## Decision

Use a **single ACM wildcard `*.cd.percona.com`** in `us-east-1`, DNS-validated
against the existing public hosted zone `Z1H0AFAU7N8IMC`. Native AWS Load
Balancer Controller integration via `alb.ingress.kubernetes.io/certificate-arn`.

cert-manager is **not** installed in v1 — see ADR 0007.

## Consequences

- Renewal is AWS-managed; no in-cluster controller in the cert path on day one.
- One cert covers everything under `cd.percona.com` — adding a new hostname is
  just a Route 53 record + Ingress, no cert work.
- ACM does not issue certs to private endpoints (no in-cluster mTLS); see ADR
  0007 for when cert-manager comes back.
