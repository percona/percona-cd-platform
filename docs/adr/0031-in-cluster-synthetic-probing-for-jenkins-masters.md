<!-- Copyright (C) 2026 Percona LLC -->
# 0031 — In-cluster synthetic probing for the Jenkins masters

**Status:** Accepted (2026-06-10)
**Related:** [ADR 0013](0013-push-from-masters-with-nginx-bearer.md) (push model), [ADR 0009](0009-scrape-vs-remote-write-for-jenkins-fleet.md) (why pull was unreachable), [ADR 0019](0019-shared-alb-ssl-termination-for-jenkins-masters.md) (EKS fronting), [ADR 0016](0016-lgtm-only-metrics-stack.md) (LGTM-only stack), [ADR 0027](0027-baked-jenkins-controller-image.md) (in-cluster ps3-k8s controller).

## Context

The masters have metrics dashboards, but nothing observes their uptime. Each master pushes the `hetzner_*` series and its `jenkins.log` ([ADR 0013](0013-push-from-masters-with-nginx-bearer.md)), and that is the whole signal: no `up` metric, no record of when a master went down or for how long, no availability number. "Is a master up" is inferred from push freshness, which cannot tell a down master from a dead master-side Alloy, an expired bearer, or a gateway fault. The Mimir ruler holds zero alert rules.

Two facts changed since ADR 0013 ruled out cluster-to-master connectivity:

- The Terraform migrations gave every EC2 master a non-colliding CIDR and peering to the EKS VPC, and the jenkins-endpoint-reconciler maintains a per-master Service on `:8080` for the ingress path. A cluster-to-master HTTP path now exists as a side effect of how users reach the masters.
- The probing hooks are already deployed and unused: the Probe CRD, the Alloy DS `prometheus.operator.probes` component (selector-free, forwarding to Mimir), and `mimir.rules.kubernetes`. Only the prober is missing.

The new path still does not make metrics pullable. `/hetzner-prometheus` bypasses Jenkins authentication (an `UnprotectedRootAction`, so the on-box Alloy can scrape it without credentials) and compensates with a peer gate inside the endpoint: any caller that is not `127.0.0.1` gets a 403, so plugin metrics remain readable only from the master itself and keep flowing through the push pipeline. That gate is deliberate and stays. Liveness needs none of it: `/login` answers 200 to anonymous callers on every master, which is all a probe requires.

## Decision

Ship a `jenkins-uptime` addon: one stateless blackbox-exporter, Probe CRs for every master, four recording rules, and a "Jenkins Uptime" Grafana dashboard. The addon is a pure consumer of the deployed hooks; Alloy and Mimir are unchanged.

### Two probe paths per master

- **internal**: `http://jenkins-<inst>.jenkins-ingress.svc:8080/login` over the peering, with the Host header the ingress NGINX would set (blackbox `hostname` parameter). Master health, ingress excluded.
- **external**: `https://<inst>.cd.percona.com/login` through DNS, the public ALB, NGINX, and the peering. The real user path; also yields `probe_ssl_earliest_cert_expiry`.

Internal up with external down means an ingress problem; both down means the master. ps3-k8s is probed through its in-cluster Service; pg (CloudFormation, no peering Service) is external-only.

### Probe series join the push pipeline on `master`

Probe targets carry the push-label convention (`<inst>.cd`, `ps3-k8s`), so the rules join both signal sources: `jenkins:master_http_up` (per master and path), `jenkins:master_last_push_age_seconds` (a `[24h:5m]` subquery over `timestamp()`, so staleness cannot hide a dead push pipeline), `jenkins:master_push_fresh`, and `jenkins:master_healthy`. Healthy requires each expected path explicitly, with external-only masters exempted via the values inventory: an absent probe series drops the master from the rule instead of false-greening on the surviving path. HTTP reachability and push liveness now fail independently.

### Quiet-down counts as down

The module accepts only HTTP 200. A master in quiet-down or still booting answers 503 and is counted as down: the probe measures user-visible availability, not process existence.

### Alerting is deferred

The Mimir Alertmanager has no receivers, and alert `for:` durations should be tuned on observed probe flap. A follow-up adds the alert group (MasterDown, MasterDegraded, PushPipelineStale, TLSCertExpiringSoon, RestartStorm) and a Slack receiver.

### In-cluster vantage, shared fate accepted

A whole-cluster outage takes the prober and the ruler with it, so it shows as absent data rather than false green. Acceptable for now: since the EKS fronting, a dead cluster already means user-visible master unavailability. A truly external backstop (Route53 health checks, or a Lambda-to-CloudWatch prober on the [ADR 0030](0030-account-cleanup-reapers-in-terraform.md) pattern) is deferred.

## Consequences

- **(+)** First direct uptime/downtime signal for all 10 masters, stored in Mimir next to every other fleet metric.
- **(+)** Failures localize: internal vs external separates master incidents from ingress incidents; probe vs push separates master incidents from observability-pipeline incidents.
- **(+)** TLS expiry is watched; pg's on-box certificate is the one that can lapse.
- **(+)** No new stateful components; the addons ApplicationSet registers the addon automatically.
- **(−)** No external vantage point: a full-cluster outage is absent data, not a firing alert, until the backstop lands.
- **(−)** Planned maintenance shows as downtime by design; the follow-up alert annotations must say so.
- **(−)** External probes hairpin through NAT and the public ALB. The cost is negligible and the real-path coverage is the point, but their failure domain includes NAT and ALB by construction.

## Alternatives considered

- **Jenkins prometheus/metrics plugin on the masters.** Installed on 0/10 masters; [ADR 0009](0009-scrape-vs-remote-write-for-jenkins-fleet.md) already judged the path unreachable, and liveness needs no master-side change. Rejected.
- **Gatus / Uptime Kuma.** A second UI, state store, and alerting path beside Grafana/Mimir. Rejected.
- **Lambda or Route53 health checks as the primary monitor.** Results land in CloudWatch, outside the store the dashboards use, or share cluster fate if pushed through the gateway. Right shape for the backstop, wrong for the primary. Deferred.
- **Scraping `/hetzner-prometheus` over the peering.** Loopback-only by design; that protection stays. Rejected.
- **node_exporter / journald from the masters.** Host telemetry, orthogonal to reachability. Out of scope.

## Verification

- `scripts/check-uptime-queries.py` runs every recording rule and dashboard query against Mimir: pre-deploy it proves parse validity and source data (10 masters), post-deploy every query must return series.
- `kubectl -n jenkins-uptime get probes` lists 19; Mimir returns `probe_success` for 9 internal and 10 external targets, and `jenkins:master_healthy` returns 10 series.
- Grafana folder "Jenkins" renders "Jenkins Uptime" with all 10 masters in the `$master` variable.
- Negative test: a master restart drops `jenkins:master_http_up` on both paths for the duration of the restart.
