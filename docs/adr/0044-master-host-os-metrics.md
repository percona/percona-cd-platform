<!-- Copyright (C) 2026 Percona LLC -->
# 0044 — Host OS metrics from the EC2 Jenkins masters via the Alloy unix exporter

**Status:** Proposed (2026-06-28)
**Supersedes (in part):** [ADR 0031](0031-in-cluster-synthetic-probing-for-jenkins-masters.md) listed "node_exporter / journald from the masters ... host telemetry ... Out of scope" as a rejected alternative. That one scope line is reversed here: host OS metrics from the EC2 masters are now in scope. The rest of ADR 0031 (uptime probing) stands.
**Related:** [ADR 0009](0009-scrape-vs-remote-write-for-jenkins-fleet.md) (why pull is unreachable), [ADR 0013](0013-push-from-masters-with-nginx-bearer.md) (push model + bearer), [ADR 0016](0016-lgtm-only-metrics-stack.md) (LGTM-only stack), [ADR 0027](0027-baked-jenkins-controller-image.md) (in-cluster ps3-k8s controller).

## Context

The platform observes three distinct node populations, each by a different mechanism:

- **EKS worker nodes.** The `prometheus-node-exporter` DaemonSet exposes full host OS metrics (`node_cpu_*`, `node_memory_*`, `node_filesystem_*`, `node_network_*`, `node_load*`). The in-cluster Alloy DaemonSet discovers its ServiceMonitor and `remote_write`s to the in-cluster Mimir distributor with `cluster="percona-ci-platform"`. This is a pull (scrape) inside the cluster and is fully live.
- **EC2 Jenkins masters.** Each master runs Grafana Alloy as a systemd unit ([ADR 0013](0013-push-from-masters-with-nginx-bearer.md)). It scrapes the Hetzner plugin's loopback `/hetzner-prometheus` endpoint and tails `jenkins.log`, then `remote_write`s and `loki.write`s to the internet-facing `mimir-push` / `loki-push` ALB, authenticated by a bearer token, terminating at the `alloy-gateway` fan-in. Uptime is observed separately by in-cluster blackbox probes ([ADR 0031](0031-in-cluster-synthetic-probing-for-jenkins-masters.md)). What is missing is **host OS metrics**: there is no `node_exporter` and no `prometheus.exporter.unix` anywhere in the master boot path.
- **Hetzner ephemeral workers.** Provisioning and health are reported by the patched plugin's `/hetzner-prometheus` series, scraped on the owning master. These short-lived VMs carry no host agent and are out of scope here, both because the provisioning signal already exists and because per-instance node series on a high-churn fleet would churn Mimir cardinality.

The result is a blind spot: when a master saturates CPU, fills a disk, or runs low on memory, nothing in the store shows it. Triage means logging into the box. Pull is not an option for the masters: they are out-of-cluster, span five AWS regions, are EIP-less with dynamic public IPv4, and have no inbound metrics path. The VPC peering added for the ingress path reaches only the master's `:8080` Jenkins UI, not a metrics port, and the public IPv4 is dynamic across the regions, so there is no stable pull target. [ADR 0009](0009-scrape-vs-remote-write-for-jenkins-fleet.md) and [ADR 0013](0013-push-from-masters-with-nginx-bearer.md) already settled this: the masters push.

## Decision

Add Grafana Alloy's built-in `prometheus.exporter.unix` to the master-side Alloy config (`scripts/install-master-observability.sh`), scrape it on loopback, and forward it through the **existing** `prometheus.remote_write "mimir"`. Alloy is already installed and running on every master, so this adds no new package, systemd unit, listening port, credential, or inbound path. The new series inherit the existing external labels (`master="<inst>.cd"`, `fleet`, `role`), so each host is queryable on its own.

Two design choices keep the footprint small and safe:

- **Curated collector set, not the default.** `set_collectors = ["cpu", "meminfo", "filesystem", "diskstats", "netdev", "loadavg", "uname", "vmstat"]`. The default set pulls in `dmi`, `hwmon`, `rapl`, `nfs`, `nvme`, `thermal_zone`, `textfile`, and more, which are noise or root-only under `User=alloy`. `netdev` is given an explicit `device_exclude` for `veth.*|docker.*|br-.*|cni.*|lo` (node_exporter has no default veth/bridge exclusion and the masters run Docker), and `filesystem` excludes pseudo and container filesystems. The result is on the order of a few hundred series per master, against the single shared Mimir tenant whose `max_global_series_per_user` is 1,500,000. The scrape uses a 60s interval and a 15s timeout, matching the existing Hetzner scrape.

- **Fail-safe rollout in the same script.** The config is generated deterministically from a heredoc, so the only escape for a bad config is a code bug, which CI catches. At runtime the script backs up the previous config before rewriting, then (capability-guarded, so older Alloy degrades gracefully) runs `alloy validate`, then `restart`s Alloy and checks `is-active`, rolling back to the backup on any failure. The bearer is pre-fetched best-effort so validation sees a complete config. The fix corrects a latent issue: the old tail was `systemctl enable --now alloy`, which only starts a stopped unit, so a re-run on a live master rewrote the config but never reloaded it.

## Rollout

Masters are stateful pets, never replaced for a metrics change. The boot script runs only at launch, so the change is applied in place:

1. Merge the script + this ADR. After merge, bump the SHA-pinned raw URL in `terraform/modules/jenkins-master/user-data.sh.tftpl` so future boots and replacements fetch the new script.
2. Re-run the updated, pinned script on each running master via SSM. It validates the regenerated config and restarts Alloy. Announce in `#opensource-jenkins` first, pre-flight `alloy --version`, apply to one master (canary), verify, then fan out a few at a time.
3. The pin bump cannot replace a master: `aws_instance.master` sets `ignore_changes = [ami, user_data, user_data_base64, launch_template[0].version]`. Gate `tf-apply` on a plan that shows no `aws_instance.master` replacement.
4. `pg` (the remaining CloudFormation master) boots from a separate vendored copy of this script in `Percona-Lab/jenkins-pipelines`, pinned at its own SHA. Giving pg host OS metrics means porting this change into that copy and re-pinning it there, tracked separately, not a one-line URL bump. `ps3` is already an in-cluster pod (see below), so the EC2 script does not apply to it.

## In-cluster convergence (why this is transitional)

The fleet is migrating from EC2 masters to in-cluster controller pods (the pet-to-k8s migration, [ADR 0027](0027-baked-jenkins-controller-image.md); `ps3-k8s` is the first). **Once a master is a pod, its host OS metrics come from scraping, internally, with no master-side push:**

- The pod lands on an EKS node already covered by the `prometheus-node-exporter` DaemonSet, so that node's `node_*` host metrics are scraped in-cluster and land in Mimir today, no per-controller change required.
- The controller's own application metrics are collected in-cluster (a pod-local Alloy sidecar writing directly to the in-cluster Mimir distributor, as `ps3-k8s` does), so the `alloy-gateway`, the bearer, and the public push ALB drop out of the path entirely.

So the `prometheus.exporter.unix` added to the EC2 master-side Alloy is a **transitional** measure: it covers each master for as long as it remains EC2, and retires automatically when that master converts to a pod (the boot script no longer runs). The end state is internal scraping for every controller. One caveat: the DaemonSet's host metrics are node-attributed, not controller-attributed; if per-controller host attribution is ever needed in-cluster, it comes from kubelet / cAdvisor pod metrics, not node_exporter.

## Consequences

- **(+)** First host OS metrics for the EC2 masters (CPU, memory, disk, inodes, network, load), labelled per master, next to every other fleet series in Mimir.
- **(+)** Reuses the existing push pipeline. No new agent, port, credential, inbound path, or network change.
- **(+)** Curated collectors plus `netdev`/`filesystem` excludes keep the cost to a few hundred series per master, negligible against the tenant cap.
- **(+)** The re-run path now validates and restarts, with automatic rollback, fixing the silent "config rewritten but not reloaded" gap.
- **(−)** The series share the single anonymous Mimir tenant's quota with cluster metrics (multitenancy is off). The added volume is small, but it is not free headroom.
- **(−)** Transitional. The exporter is removed per master as the in-cluster migration proceeds, so this is maintenance that has a planned end, not a permanent fixture.
- **(−)** The master-side Alloy still does not self-scrape, so the exporter's own send-side health is not centrally visible; confirm per master with `curl localhost:12345/metrics` or the fleet freshness gate.

## Alternatives considered

- **Standalone `node_exporter` binary scraped by local Alloy.** A second package, a systemd unit, and a listening port for no benefit, since Alloy already runs and ships a built-in unix exporter. Rejected.
- **Direct Mimir/Prometheus pull of a master node_exporter.** Unreachable by construction ([ADR 0009](0009-scrape-vs-remote-write-for-jenkins-fleet.md)): out-of-cluster, multi-region, dynamic IPv4, no inbound metrics path, peering scoped to the `:8080` UI. Rejected.
- **Wait for in-cluster convergence and skip the masters.** Leaves every EC2 master blind to host saturation for the open-ended duration of the migration. Rejected.
- **The default collector set.** Noisy and higher-cardinality (`dmi`, `hwmon`, `rapl`, `nfs`, `nvme`, and unfiltered `netdev` veth churn), some root-only under `User=alloy`. Rejected for the curated set above.

## Verification

- Pre-flight: `count by (master) (node_uname_info)` returns no master-labelled series (baseline).
- On a canary master: `alloy validate /etc/alloy/config.alloy` is clean and `systemctl is-active alloy` is active. The `node` scrape shows healthy in Alloy's local component view at `localhost:12345`; authoritative confirmation is the Mimir query below.
- In Mimir: after rollout, `count by (master) (node_uname_info)` lists every rolled-out master; `node_memory_MemAvailable_bytes{master="<inst>.cd"}` and `rate(node_cpu_seconds_total{master="<inst>.cd"}[5m])` return data.
- `just check-master-alloy` (dynamic master set) stays green; extend it with a `node_*` presence check.
- Negative test: no `veth*`, `docker*`, or `br-*` device labels appear in `node_network_*`.
