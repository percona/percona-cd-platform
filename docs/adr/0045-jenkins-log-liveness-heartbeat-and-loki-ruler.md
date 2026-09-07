<!-- Copyright (C) 2026 Percona LLC -->
# 0045 - Jenkins log liveness: a heartbeat line per master and Loki-ruler recording rules in Mimir

**Status:** Proposed (2026-09-07)
**Amends:** [ADR 0031](0031-in-cluster-synthetic-probing-for-jenkins-masters.md), which deferred every alert rule until probe flap had been observed. One alert lands here because it does not depend on probe flap. The uptime alert group stays deferred.
**Related:** [ADR 0013](0013-push-from-masters-with-nginx-bearer.md) (masters push jenkins.log through alloy-gateway), [ADR 0016](0016-lgtm-only-metrics-stack.md) (Mimir's ruler is the sole rule evaluator for metrics), [ADR 0044](0044-master-host-os-metrics.md) (master-side Alloy is edited only through the pinned install script).

## Context

On 2026-05-13 the ps80 controller stopped writing INFO lines to `jenkins.log` about 75 minutes after a JVM start and stayed silent for 14.5 hours while it kept pushing metrics. Alloy had nothing to tail, Loki showed a gap, and nothing noticed until a person looked. A reboot cleared it, the trigger was never found, and it has not recurred on the nine Terraform masters or the in-cluster controller since.

The obvious detector, "no log line for N minutes", does not work on this fleet. An idle master is legitimately silent for hours: over one September week ps57, ps80 and pxb wrote nothing in 127, 70 and 102 of the 168 hours, and their file line counts matched what Loki held hour for hour. A silence alert needs a floor.

Two places could carry the measurement. The master-side Alloy could push its own `loki.source.file` counters, but that config lives in the install script pinned by commit in the master module's user-data, so a change there re-renders user-data for every master. The Loki ruler can count lines per master with LogQL, and it can remote-write the result to Mimir, where the existing `jenkins:master_push_fresh` series already says whether a master is alive.

## Decision

- **A heartbeat line per minute.** `logHeartbeat.groovy` in every master's `init.groovy.d` (the nine EC2 masters via their S3 init buckets, ps3-k8s via its boot-time init ConfigMap) registers a `PeriodicWork` that writes one INFO line per minute through the root `java.util.logging` logger, the same path as Jenkins' own INFO output. Whatever silences INFO silences the heartbeat. Cost: about 100 bytes per minute per master.
- **Loki ruler records lines per master into Mimir.** The Alloy DS gains a `loki.rules.kubernetes` sync for PrometheusRule resources labelled `percona.com/ruler: loki`, and the Mimir sync excludes that label so LogQL never reaches Mimir's ruler. The Loki ruler remote-writes to the Mimir distributor. First rule: `jenkins:master_log_lines:count15m`, scoped by the jenkins-uptime masters inventory so fenced clones stay out.
- **Mimir joins and alerts.** `jenkins:master_logs_fresh` is 1 when a line arrived in the last 15 minutes and 0 when the master still has a push-freshness series but no line. `JenkinsMasterLogsSilent` fires after a further 5 minutes when logs are stale while metrics still arrive, so a master that is down entirely does not also fire here. The jenkins-uptime dashboard shows the same series.
- **Delivery stays where ADR 0031 left it.** The Mimir Alertmanager routes to a receiver with no integration. The alert is visible in Alertmanager, the ruler API and the dashboard until a Slack receiver lands.

## Consequences

- **(+)** A silent JUL root logger, a detached file handler, a dead master-side Alloy tail, an expired bearer on the loki-push path, and a broken in-cluster tail all surface as the same signal within about 20 minutes, per master.
- **(+)** The Loki ruler to Mimir path is reusable: any LogQL measurement can become a PromQL series with one labelled PrometheusRule.
- **(−)** ps3-k8s fires from day one. Its controller stdout is not in Loki at all and its sidecar's build-events stream stopped on 2026-08-21, which is exactly the class of fault this rule exists for. Tracked separately.
- **(−)** A Loki ruler restart leaves a short remote-write gap, which the alert's `for` absorbs. A Mimir ruler outage silences the alert together with every other rule.
- **(−)** Two rule syncs now watch the same CRD kind, distinguished by one label. A Loki-bound rule without the label is rejected by Mimir at POST time and shows up in the Alloy DS log.

## Alternatives considered

- **Master-side Alloy self-metrics** (`loki_source_file_*`): the honest per-file counter, rejected for now because the master-side config is pinned in user-data and a change there touches every master's launch path.
- **A textfile or `node_filesystem` proxy for file growth**: measures the file, not the pipeline, and still needs a heartbeat to separate idle from dead.
- **Grafana-managed alert rule on a Loki datasource query**: no precedent in this repo, and a per-master absent check in Grafana expressions is awkward. The Mimir ruler already holds the fleet's rules.
