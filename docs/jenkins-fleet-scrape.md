# Jenkins Fleet Scrape

The page name is historical. The fleet does not get scraped any more: each
Jenkins master ships its own metrics and logs over outbound 443 to the
in-cluster `alloy-gateway`. Pull was retired in
[ADR 0013](adr/0013-push-from-masters-with-nginx-bearer.md); the
end-to-end pipeline was canaried on `ps3.cd` on 2026-05-08.

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
`percona-ci-platform/alloy-gateway/bearer` (one-time `openssl rand -hex
32`, KMS-encrypted by the account default key).

- **Master side**: cloud-init pulls the token to `/etc/alloy/gateway-token`
  (mode 0400) using the master's own instance profile. The IAM policy
  attached for this is scoped to
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
  default in `resources/addons/alloy-gateway/values.yaml` and is slated
  to populate from a cluster-secret annotation (the 9 EC2 master EIPs
  plus 3 cluster NAT-GW EIPs) in a follow-up. Bearer is the active gate
  today; CIDR is the planned defence-in-depth layer.

Bearer rotation is a single Secrets Manager update plus
`systemctl reload alloy` on each master and a rolling restart of the
gateway nginx sidecar.

## Master-side Alloy config (sketch)

`/etc/alloy/config.alloy` on each master has four components:

```
prometheus.scrape "hetzner_local" {
  targets    = [{ "__address__" = "localhost:8080", "__metrics_path__" = "/hetzner-prometheus/" }]
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

## Canary verification (ps3.cd, 2026-05-08)

End-to-end push pipeline live. Verified against Mimir / Loki via
Grafana datasources:

- `hetzner_api_rate_limit_remaining{master="ps3.cd"} = 3599` — metrics
  path: master `prometheus.scrape "hetzner_local"` → ALB → NGINX bearer
  → in-Pod Alloy → Mimir distributor → ingester → S3.
- `{master="ps3.cd"}` returns Jenkins SLF4J lines from
  `/var/log/jenkins/jenkins.log` — log path: master `loki.source.file
  "jenkins"` → ALB → NGINX bearer → in-Pod Alloy → Loki distributor →
  ingester → S3.

The two stack-isolation work-items behind the canary are tracked in the
follow-up ADRs (forthcoming): the `master` label fix that disambiguates
EC2 from in-cluster Jenkins masters, and the memberlist `cluster_label`
isolation between Mimir / Loki / Tempo gossip rings (see
`resources/addons/{mimir,loki,tempo}/values.yaml`).

## Phase pointers

- **Phase A.2** — fleet rollout to the other 8 EC2 masters (`pmm`,
  `ps80`, `pxc`, `pxb`, `psmdb`, `pg`, `ps57`, `rel`, `cloud`). Same
  `config.alloy`, same instance-profile policy, same bearer; per-master
  TF/CFN deltas only.
- **Phase B** — activate `v103.percona.11` on the masters still pinned
  to `v103.percona.9` / `.10`. Pin-only on busy masters, force-restart
  on idle, same release procedure as previous Hetzner-plugin rolls.
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
