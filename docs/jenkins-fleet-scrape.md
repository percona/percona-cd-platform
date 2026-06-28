# Jenkins Fleet Scrape

The page name is historical. The fleet does not get scraped any more: each
Jenkins master ships its own metrics and logs over outbound 443 to the
in-cluster `alloy-gateway`. Pull was retired in
[ADR 0013](adr/0013-push-from-masters-with-nginx-bearer.md); the
end-to-end pipeline was canaried on `ps3.cd` on 2026-05-08, and rolled out to all 10 masters via `Percona-Lab/jenkins-pipelines` PR #4037 (commit 83adb97) on 2026-05-10.

## Why push, not pull

The pull design in [ADR 0009](adr/0009-scrape-vs-remote-write-for-jenkins-fleet.md)
required the cluster Prometheus to reach each master over public DNS, with
NAT-GW EIPs allowlisted on each master's SG and (later) VPC peering for
private connectivity. The peering plan ran into a four-way CIDR collision
on `10.177.0.0/22` (`cloud`, `ps3`, `ps57`, `pxc` all share the same
`/22`); see [`connectivity.md`](connectivity.md) and ADR 0009 for the
detail. Only one of those four masters could ever peer with the cluster
VPC; the other three were stranded on public scrape forever.

Pushing inverts the direction of flow. The cluster never dials a master.
Outbound 443 from each master to one ALB hostname needs no inbound route
back, so the CIDR collision becomes irrelevant. One Alloy unit per master
covers both metrics and logs over the same egress hole.

## Topology

- master `alloy.service` (systemd, Amazon Linux 2023 `dnf install -y alloy`) →
- ALB at `mimir-push.cd.percona.com`, `loki-push.cd.percona.com`,
  `tempo-push.cd.percona.com` (group `jenkins-cd`, ACM wildcard) →
- alloy-gateway Pod's NGINX bearer-auth sidecar (`nginx:1.27-alpine`) →
- in-Pod Alloy receivers on loopback: `prometheus.receive_http` on
  `127.0.0.1:19009`, `loki.source.api` on `127.0.0.1:13100`,
  `otelcol.receiver.otlp` HTTP on `127.0.0.1:14318` →
- Mimir / Loki / Tempo distributors via in-cluster Service DNS →
- their ingesters and S3 (see [`observability.md`](observability.md)).

Two Alloy gateway replicas behind one Service per receiver. The cluster
has no inbound path from masters except those three ALB hostnames.

## Endpoints and paths

| Hostname | Path | Notes |
|---|---|---|
| `mimir-push.cd.percona.com` | `/api/v1/metrics/write` | NOT `/api/v1/push`. Alloy's `prometheus.receive_http` accepts the metrics-write path; the canary first failed on `/api/v1/push` until the master-side `prometheus.remote_write` URL was corrected. |
| `loki-push.cd.percona.com` | `/loki/api/v1/push` | Standard Loki push API; Alloy's `loki.source.api` handles it. |
| `tempo-push.cd.percona.com` | `/v1/traces` | OTLP HTTP. OTLP gRPC (`:4317`) stays cluster-internal; ALB target-group health checking does not yet support gRPC. |

## Auth model

Bearer in the `Authorization` header. One token, shared across the fleet,
stored in AWS Secrets Manager at
`percona-ci-platform/alloy-gateway/bearer`. The `SecretString` value is
JSON `{"bearer_token":"..."}` (not plain string); every consumer
(master-side fetcher, `scripts/verify-observability.sh`, ESO `dataFrom:
extract`) JSON-parses `.bearer_token` before use.

- **Master side**: systemd `alloy.service` invokes
  `ExecStartPre=+/usr/local/bin/alloy-fetch-token` -- the `+` prefix runs
  the fetcher as root because `/etc/alloy/` is `0750 root:alloy` while
  alloy itself runs `User=alloy`. The fetcher reads the JSON
  `SecretString`, extracts `.bearer_token`, and writes it to
  `/etc/alloy/gateway-token` (mode 0440, owner `root:alloy`) atomically
  (write to `.tmp`, then `mv -f`). It re-runs on every alloy start, so
  rotation is a `systemctl restart alloy` away -- no cloud-init re-run.
  The IAM policy `AlloyGatewayBearerRead` (attached to every
  `jenkins-<inst>-master` role) is scoped to
  `secretsmanager:GetSecretValue` on the bearer ARN only,
  `arn:aws:secretsmanager:us-east-1:119175775298:secret:percona-ci-platform/alloy-gateway/bearer-*`.
- **Cluster side**: External Secrets Operator syncs the same secret into
  Kubernetes Secret `alloy-gateway-bearer` in the `alloy-gateway`
  namespace. The NGINX sidecar reads the token via env var
  (`BEARER_TOKEN`), validates `Authorization: Bearer <token>`, then
  **strips the `Authorization` header** before `proxy_pass` to loopback,
  so the inner Alloy never sees the secret.
- **ALB CIDR allowlist** (`alb.ingress.kubernetes.io/inbound-cidrs`)
  remains the second, independent layer. It is intentionally empty by
  default in `resources/addons/alloy-gateway/values.yaml`. The original
  plan to populate it from the 9 master EIPs died with the EIP removal
  (masters now egress from dynamic public IPv4s); an allowlist needs a
  stable-egress design (NAT or pinned egress IPs) first. Bearer is the
  active gate today; CIDR stays the planned defence-in-depth layer.

Bearer rotation is a single Secrets Manager update plus
`systemctl reload alloy` on each master and a rolling restart of the
gateway nginx sidecar.

## Master-side Alloy config (sketch)

`/etc/alloy/config.alloy` on each master has the following components:

```
prometheus.scrape "hetzner_local" {
  targets    = [{ "__address__" = "localhost:8080", "__metrics_path__" = "/hetzner-prometheus/" }]
  forward_to = [prometheus.remote_write.mimir.receiver]
  scrape_interval = "60s"
}

prometheus.exporter.unix "node" {
  set_collectors = ["cpu", "meminfo", "filesystem", "diskstats", "netdev", "loadavg", "uname", "vmstat"]
  // netdev/filesystem excludes drop Docker veth + pseudo-fs noise; see ADR 0044.
}

prometheus.scrape "node" {
  targets    = prometheus.exporter.unix.node.targets
  forward_to = [prometheus.remote_write.mimir.receiver]
  scrape_interval = "60s"
}

prometheus.remote_write "mimir" {
  endpoint {
    url = "https://mimir-push.cd.percona.com/api/v1/metrics/write"
    bearer_token_file = "/etc/alloy/gateway-token"
  }
  external_labels = {
    master = "ps3.cd",
    fleet  = "percona-jenkins",
    role   = "master",
  }
}

loki.source.file "jenkins" {
  targets    = [{ __path__ = "/var/log/jenkins/jenkins.log", master = "ps3.cd", fleet = "percona-jenkins", role = "master" }]
  forward_to = [loki.write.gateway.receiver]
}

loki.write "gateway" {
  endpoint {
    url = "https://loki-push.cd.percona.com/loki/api/v1/push"
    bearer_token_file = "/etc/alloy/gateway-token"
  }
}
```

The `master = "<inst>.cd"` convention (e.g. `ps3.cd`, `pmm.cd`) is
deliberate: it avoids label collision with the `<inst>-k8s` masters from
the in-cluster POC (e.g. `ps3-k8s.cd.percona.com`), which use
`master = "<inst>-k8s"` from a Prometheus scrape inside the cluster. The
two label spaces stay disjoint.

## Hetzner-plugin scrape target

`localhost:8080/hetzner-prometheus/` (note the trailing slash; without it
Stapler returns a 302 to the canonical form and Alloy logs the redirect).
The endpoint is bound to the Jenkins loopback interface (Jenkins serves
`8080` on `127.0.0.1` only, per the existing master config), so no
external client can reach it.

The plugin version that makes anonymous loopback work is
**`v103.percona.11`**. `v103.percona.10` was insufficient: it only
removed the `Jenkins.SYSTEM_READ` check inside `doIndex()`, but
`GlobalMatrixAuthorizationStrategy` still rejected anonymous callers
with HTTP 403 before the request ever reached `doIndex()`. v11 changes
`HetznerPrometheusEndpoint` to `implements UnprotectedRootAction`, which
moves the endpoint outside Jenkins' core authorization gate entirely.
The trust boundary is the loopback bind, not Jenkins ACLs.

## Fleet verification (10/10 masters, 2026-05-10)

End-to-end push pipeline live on every master. `Percona-Lab/jenkins-pipelines`
PR #4037 (commit 83adb97) codified `alloy.service` + `amazon-ssm-agent` + the
scoped `AlloyGatewayBearerRead` IAM policy on every `jenkins-<inst>-master`
role via CFN (9 masters) and Terraform (`pxb.cd`). The split has since
inverted: the jenkins-master module carries the same policy for the 8 TF
masters and only `pg.cd` remains CFN.

Verified against Mimir / Loki via `scripts/check-master-ingest.sh`: each
master pushes ~180 datapoints per 3 hours on `hetzner_api_rate_limit_remaining`
(one sample per 60s scrape), 10/10 series present, fail counter 0 across the
fleet, log volume tracking each master's actual Jenkins activity.

Ongoing fleet freshness is gated by `just check-master-alloy`
(`scripts/check-master-alloy-mimir.py`), which keys on the same gauge but
derives the expected master set from repo source-of-truth, so the "10/10"
count cannot silently rot as masters come and go.

### Auth model (push pipeline)

Bearer secret value at `percona-ci-platform/alloy-gateway/bearer` is JSON
`{"bearer_token":"..."}` (not plain string); every consumer must JSON-parse
before use. The master-side `/usr/local/bin/alloy-fetch-token` runs via
systemd `ExecStartPre=+/usr/local/bin/alloy-fetch-token` -- the `+` prefix
elevates to root because `/etc/alloy/` is `0750 root:alloy` while alloy
itself runs `User=alloy`. The fetcher re-runs on every alloy start, so
secret rotation is a `systemctl restart alloy` away.

### Rotation forcing pattern

On `pg.cd` (the one remaining CFN spot master), a change-set that updates the
`LaunchTemplate` version without forcing SpotFleet replacement (e.g., a
userData-only diff) needs `aws ec2 terminate-instances` on the active master;
SpotFleet re-launches on the new template and the EIP follows automatically,
so DNS stays stable. The 8 TF masters run on-demand `aws_instance` with
`ignore_changes = [user_data, ami]`: a userData or AMI diff only bumps the
launch-template version and lands on the NEXT instance replacement, never on
a plain apply (no `terraform taint` needed). Their DNS is ALB-fronted and
does not track instance IPs.

## Phase pointers

- **Phase A** — fleet rollout complete (PR #4037, 2026-05-10). See above.
- **Phase B** — activate `v103.percona.11` on any masters still pinned to
  `v103.percona.9` / `.10`. Post-rollout instances re-installed the plugin
  at boot, so this only affects masters that have not rotated since the
  plugin pin landed. Spot-check via `jenkins hetzner --all` plugin-version
  probe.
- **Phase C** — extend the push model to workers. The hetzner cloud
  plugin's per-template metrics, EC2 plugin once instrumented
  (PS-10996 successor), and PMM agents share the same gateway under
  separate Mimir tenants if multi-tenancy ever matters.

## Related decisions

- [ADR 0013 — push from masters with NGINX bearer](adr/0013-push-from-masters-with-nginx-bearer.md)
  (this page's authority).
- [ADR 0009 — scrape vs remote_write for Jenkins fleet](adr/0009-scrape-vs-remote-write-for-jenkins-fleet.md)
  (superseded; useful for the CIDR-collision history).
- [ADR 0010 — distributed LGTM](adr/0010-distributed-lgtm.md) (the
  alloy-gateway Pod and the receiver Ingresses).
- ADR 0014 / ADR 0015 (forthcoming) — `master` label disambiguation and
  memberlist `cluster_label` isolation.
