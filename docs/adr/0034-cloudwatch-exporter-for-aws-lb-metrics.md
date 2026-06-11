<!-- Copyright (C) 2026 Percona LLC -->
# 0034 — CloudWatch exporter for AWS load balancer metrics

**Status:** Accepted (2026-06-11)
**Related:** [ADR 0004](0004-pod-identity-default.md) (Pod Identity as the in-cluster IAM default), [ADR 0016](0016-lgtm-only-metrics-stack.md) (LGTM-only metrics stack, Mimir ruler), [ADR 0031](0031-in-cluster-synthetic-probing-for-jenkins-masters.md) (synthetic probing of the same front door), [ADR 0013](0013-push-from-masters-with-nginx-bearer.md) (the push receivers the jenkins-cd ALB fronts).

## Context

Every external request to the platform crosses an AWS load balancer: the jenkins-cd ALB (Authentik, Grafana, ArgoCD, Headlamp, the alloy-gateway push receivers), the jenkins-masters ALB (every Jenkins master hostname), and the jenkins-ps3-k8s agent NLB (inbound JNLP). Their traffic and target-health telemetry (request counts, ELB-vs-target 4XX/5XX split, TargetResponseTime, RejectedConnectionCount, UnHealthyHostCount) exists only in CloudWatch's `AWS/ApplicationELB` and `AWS/NetworkELB` namespaces.

Nothing in the stack reads CloudWatch. Grafana has no CloudWatch datasource, Mimir holds zero `aws_*` series, and front-door incidents are invisible: ADR 0031's probes answer "is it up from outside", not "what is the load balancer itself seeing".

## Decision

- Deploy prometheus-community's **yet-another-cloudwatch-exporter (YACE) v0.65.0** as the `cloudwatch-exporter` addon (chart `prometheus-yet-another-cloudwatch-exporter` 0.45.0), a single-replica Deployment whose ServiceMonitor rides the existing Alloy discovery into Mimir.
- **Tag-scoped discovery**, never region-wide: ALBs match the LBC tag `elbv2.k8s.aws/cluster=percona-ci-platform`, the legacy-CCM NLB matches `kubernetes.io/cluster/percona-ci-platform=owned`. Other teams' load balancers in the account stay out.
- **Scrape-time timestamps** (`addCloudwatchTimestamp: false`): Mimir's `out_of_order_time_window` stays 0 and `nilToZero` keeps working. The cost is structural lag (values trail reality by 5-12 min) and gauge semantics: each series holds the newest complete 300s bucket, so consumers divide by 300 instead of `rate()`.
- **Window tuning** `period = length = scraping-interval = 300`, `delay = 120`: one complete bucket per cycle, off the still-filling edge, at a fixed API cost (~85-95 GetMetricData metrics per cycle, $6-8/month) independent of dashboard viewers.
- **IAM via Pod Identity** on the `cloudwatch-exporter` SA: four read-only account-wide actions (`cloudwatch:GetMetricData`, `cloudwatch:ListMetrics`, `tag:GetResources`, `iam:ListAccountAliases`). `GetMetricStatistics` is deliberately absent (discovery jobs never call it).
- **No alert rules in v1.** The Mimir ruler evaluates PrometheusRules, but the Alertmanager has no receivers wired, Grafana sets `manageAlerts: false`, and the jenkins-uptime precedent defers alerts until baselines exist. Receiver wiring is its own follow-up.

## Consequences

- `aws_applicationelb_*` and `aws_networkelb_*` series live in Mimir next to everything else: dashboards, recording rules, and joins with the jenkins-uptime probes during the same incident.
- Grafana never touches AWS APIs; CloudWatch spend is a fixed function of the metric list, not of viewers.
- The 5-12 min lag is structural. Near-real-time front-door alerting additionally needs Alertmanager receivers; per-request forensics belong to the ALB access-log path (`alb-access-logs.tf` bucket, ingestion not yet built).
- A second replica would double API spend and duplicate every series; the Deployment is deliberately a singleton, and a reschedule gap is invisible at this latency.

## Alternatives considered

- **Grafana CloudWatch datasource** — zero infra, but every dashboard view fires billed `GetMetricData` calls and the data never lands in Mimir (no rules, joins, retention). May still be added later as an ad-hoc explorer; rejected as the primary path.
- **Alloy's `prometheus.exporter.cloudwatch`** (embeds YACE) — zero new workloads, but the IAM grant would widen the shared scraping Alloy's identity, and the clustered DaemonSet would instantiate the exporter per pod (duplicate series, N-fold API spend). A scoped singleton is cleaner.
- **Java `prometheus-cloudwatch-exporter`** — no tag-based discovery or GetMetricData batching parity; the community consolidated on YACE.
- **CloudWatch Metric Streams -> Firehose -> Mimir** — 2-3 min latency instead of 5-12, but requires Firehose, a public TLS ingest endpoint, and an alpha OTel receiver for one namespace in one account. Overkill at this scale.
