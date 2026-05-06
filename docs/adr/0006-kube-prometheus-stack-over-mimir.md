# 0006 — kube-prometheus-stack at v1, LGTM deferred

**Status:** Superseded (2026-05-06) by [ADR 0010](0010-distributed-lgtm.md).
The "v1 only" framing was reversed once the platform's scope expanded to
cover the durable Jenkins fleet rather than a PoC. Original text below
preserved for history.

**Original status:** Accepted (2026-04-30)

## Context

Need observability on day one: scrape EKS control-plane + node-exporter +
kube-state-metrics, collect Jenkins fleet metrics over public DNS, render
dashboards, raise alerts.

Options evaluated:

- `kube-prometheus-stack` (Prometheus operator + Prometheus + Alertmanager +
  Grafana + node-exporter + kube-state-metrics, single chart).
- `grafana/lgtm-distributed` (Loki + Grafana + Tempo + Mimir, demo-only per
  Grafana Labs README).
- Mimir alone (~6 microservices).
- Self-managed plain Prometheus (no operator, no dashboards, no alerts wiring).

## Decision

Ship **`kube-prometheus-stack` v84.4.0** as the single observability stack at
v1. Reserve `prometheus.prometheusSpec.remoteWrite` as the upgrade hook.

Skip Loki (no logs requirement; CloudWatch covers Jenkins and EKS control
plane). Skip Tempo (no traced workloads). Skip Mimir (one cluster, one tenant,
30 d retention fits in a single Prometheus pod with a 100 GiB gp3 PVC).

## Consequences

- One ArgoCD Application owns the entire metrics stack.
- Upgrade path on retention/tenant pressure: enable `remoteWrite` to **Grafana
  Cloud** (free tier 14 d, Pro 13 mo) or self-hosted Mimir. No chart swap.
- Loki / Tempo can be added later as separate addons without touching this
  one.
- See `docs/lgtm-evaluation.md` for the full triggers-to-revisit table.
