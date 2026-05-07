# 0013: Push from masters to alloy-gateway with NGINX bearer auth (Jenkins fleet)

**Status:** Accepted (2026-05-07)
**Supersedes:** [ADR 0009](0009-scrape-vs-remote-write-for-jenkins-fleet.md)'s "v1 ships Option A"
**Related:** [ADR 0010](0010-distributed-lgtm.md) (alloy-gateway), [ADR 0011](0011-robustness-pass.md) (production-readiness pass)

## Context

ADR 0009 (2026-04-30) chose Option A (cluster pulls metrics from each Jenkins
master) as the v1 path while marking Option B (master-side agent +
remote_write) as the long-term right answer, gated on PS-10543, PS-10996,
PS-10997, and SG tightening. The plan was to peer 7 of 10 master VPCs into
the cluster VPC, fall back to public + NAT-GW for the 3 colliders sharing
`10.177.0.0/22` (cloud, ps57, pxc, with ps3 winning the single peering slot),
and overlay split-DNS via a Route53 private hosted zone.

When the 2026-05-07 implementation actually started cutting Terraform for the
peering + private DNS, three things forced a re-evaluation:

1. **Operational tax of pull**: 7 cross-region peering connections +
   accepters, region providers, master-side route-table edits, Route53 zone
   associations on the cluster VPC, plus per-master CFN/TF changes to swap
   `0.0.0.0/0:443` ingress for `10.220.0.0/16`. The 4-way CIDR collision
   permanently strands 3 masters on public scrape, defeating the
   "private-by-default" framing that justified peering in the first place.
2. **The alloy-gateway is already up.** ADR 0010's L-4 wave landed
   `alloy-gateway` (alloy + 3 ALB Ingresses at `mimir-push`, `loki-push`,
   `tempo-push` under `cd.percona.com`). The push receivers (Prometheus
   remote_write, Loki push, OTLP) are already terminating at the cluster
   edge. Pull was provisioning a different path (Prometheus operator scrape
   over public DNS) when push infrastructure was already in place.
3. **One agent does double duty.** A master-side Alloy can scrape
   `localhost:8080/hetzner-prometheus`, tail `/var/log/jenkins/jenkins.log`,
   and ship both upstream over the same outbound 443. Pull needed a separate
   log shipper or accepted no log story at all (the original plan's
   Phase 2.5 had to invent a parallel master-side Alloy just for logs). Push
   collapses Phase 2 + 2.5 into one config file.

Auth was the open question on push. Alloy 1.x receivers
(`prometheus.receive_http`, `loki.source.api`, `otelcol.receiver.otlp`) have
no built-in auth (verified against Grafana Alloy 1.10 reference docs). The
gateway as shipped is `0.0.0.0/0` on three ALB rules, no auth. That cannot
fan out to 9 masters.

## Decision

**v1 ships Option B (master-side push) with NGINX bearer-token sidecar plus
ALB inbound-CIDRs allowlist at the gateway.** Two independent failure
domains for auth. One Alloy unit per master for both signals. No
cluster-to-master connectivity at all.

### B1. Push direction

Each Jenkins master runs a Grafana Alloy systemd unit. One config file
(`/etc/alloy/config.alloy`) handles:

- `prometheus.scrape "hetzner_local"` against
  `localhost:8080/hetzner-prometheus` at 60 s interval, forwarded to
  `prometheus.remote_write "mimir"` at
  `https://mimir-push.cd.percona.com/api/v1/push`.
- `loki.source.file "jenkins"` tailing `/var/log/jenkins/jenkins.log`,
  forwarded to `loki.write "gateway"` at
  `https://loki-push.cd.percona.com/loki/api/v1/push`.

External labels `master=<inst>, fleet=percona-jenkins` on the metric path;
`master=<inst>, job=jenkins, component=hetzner-plugin` labels on the log
path.

The cluster initiates no connection toward any master. The 4-way CIDR
collision on `10.177.0.0/22` is irrelevant: outbound 443 from each master to
the gateway needs no routable inbound path back.

### B2. Gateway auth (NGINX sidecar + ALB inbound-CIDRs)

Two enforcement layers, each independently sufficient:

- **Layer 1: NGINX bearer-token sidecar** in the alloy-gateway Pod.
  `nginx:alpine` listens on the receiver ports (3100, 9009, 4318), validates
  `Authorization: Bearer <token>` against a file mount of
  `/run/secrets/bearer-token`, strips the header, and proxies to
  `localhost:<same-port>` where Alloy now binds (loopback only). The
  receiver Service that backs the ALB Ingresses points at the nginx ports,
  not at Alloy's direct ports. Stripping the `Authorization` header before
  `proxy_pass` keeps the upstream Alloy unaware of the secret.
- **Layer 2: ALB inbound-CIDRs allowlist.**
  `alb.ingress.kubernetes.io/inbound-cidrs` enumerates the 9 EC2 master
  public IPs (collected per region via `paws ec2 list`) plus the 3 cluster
  NAT-GW EIPs. A leaked bearer is not exploitable from outside those CIDRs.

The bearer secret lives in AWS Secrets Manager at
`percona-ci-platform/alloy-gateway/bearer` (one-time
`openssl rand -hex 32`, KMS-encrypted by the account default key). External
Secrets Operator syncs it into Kubernetes Secret `alloy-gateway-bearer` in
the `alloy-gateway` namespace; the NGINX sidecar mounts it read-only.

Alloy-native auth was considered and rejected: Alloy 1.x receivers have no
auth blocks. NGINX is the supported path until upstream adds it.

### B3. Drop the SYSTEM_READ gate on /hetzner-prometheus

In
`cloud.dnation.jenkins.plugins.hetzner.metrics.HetznerPrometheusEndpoint.doIndex()`,
remove `Jenkins.get().checkPermission(Jenkins.SYSTEM_READ)`. The endpoint is
bound to localhost (Jenkins serves 8080 on the loopback interface only, per
the existing master config), so external clients cannot reach it; the
master-side Alloy is the only consumer. Bumps plugin version to
`v103.percona.10`.

This kills the `prom-scraper-svc` user provisioning task and the
`matrix.groovy` persistence companion PR entirely. One bearer at the gateway
replaces 10 Jenkins API tokens.

### B4. IAM: extend each master's existing instance profile

Each Jenkins master already has a dedicated instance profile (e.g.
`jenkins-ps3` at
`arn:aws:iam::119175775298:instance-profile/jenkins-ps3`). Attach a new
managed policy granting `secretsmanager:GetSecretValue` ONLY on
`arn:aws:secretsmanager:us-east-1:119175775298:secret:percona-ci-platform/alloy-gateway/bearer-*`.
Cloud-init pulls the token to `/etc/alloy/gateway-token` (mode 0400) before
the Alloy systemd unit starts.

Less invasive than provisioning a new role per master; preserves
single-identity-per-host posture.

## Consequences

- **Direction of flow inverts.** Cluster never dials masters. The 7 VPC
  peerings, the Route53 private zone, the cluster route-table edits, and
  the master-side SG tightening from ADR 0009's hybrid plan all drop. The
  4-way CIDR collision becomes a paper trail.
- **One Alloy per master** replaces "no agent on masters". Operationally
  lightweight (Amazon Linux 2023 `dnf install -y alloy`), monitored via
  `systemctl is-active alloy` and the gateway-side Alloy ServiceMonitor that
  ADR 0010's L-4 wave already provisioned.
- **Plugin endpoint becomes localhost-only.** SYSTEM_READ gate removed.
  Acceptable because Jenkins binds 8080 to the loopback (verified on ps3
  canary, 2026-05-07: `ss -ltnp | grep :8080` shows
  `127.0.0.1:8080`). If that bind ever changes, this ADR needs
  re-evaluation.
- **Bearer rotation** is a single Secrets Manager update plus
  `systemctl reload alloy` (which re-reads `bearer_token_file`) and a
  rolling restart of the gateway nginx sidecar. Per-plugin or per-master
  tokens are possible later if revocation isolation matters; v1 ships one
  shared token because the threat model (leaked plaintext) is bounded by
  the ALB CIDR allowlist.
- **Mimir tenancy header** stays on `X-Scope-OrgID: percona-ci`. Future
  tenants (PMM-agent push, EC2 plugin once instrumented) can ship to the
  same gateway under a different tenant ID without touching Hetzner data.
- **Reversibility**: each change is a values-file edit on the EKS side and
  a CFN/TF stack-update on the master side. The fallback to ADR 0009's
  hybrid scrape exists as a code path under `[SUPERSEDED]` in the
  implementation plan.
- **EC2 plugin extensibility** (PS-10996 successor): the master-side Alloy
  block list extends with one extra `prometheus.scrape` target once the
  EC2 plugin grows a `/ec2-prometheus` endpoint. Same gateway, same
  bearer, same dashboard sidecar pattern. No infrastructure change.

## Tracking

- Plugin `v103.percona.10` release: drops SYSTEM_READ gate (one-line change
  in `HetznerPrometheusEndpoint.java`), bumps `pom.xml`. Same release
  procedure as `v103.percona.9` (pin-only on busy masters, force-restart on
  idle).
- alloy-gateway chart (`resources/addons/alloy-gateway/`):
  - `templates/configmap-nginx-auth.yaml` (NEW).
  - `templates/external-secret-bearer.yaml` (NEW).
  - `templates/ingress.yaml`: add `inbound-cidrs` annotation.
  - `values.yaml`: extraContainers for `nginx:alpine`, extraVolumes, switch
    `service-receivers.yaml` Service to nginx ports.
- Per-master TF/CFN: extend instance profile with
  `SecretsManagerRead-AlloyGateway` policy plus cloud-init for
  `/etc/alloy/`.
- AWS Secrets Manager:
  `percona-ci-platform/alloy-gateway/bearer` (one-time
  `aws secretsmanager create-secret`).
- Revert: drop the `additionalScrapeConfigs` block from
  `resources/addons/kube-prometheus-stack/values.yaml` (commit `bf2f68f`
  introduced it).

Plan file:
[`/home/percona/.claude/plans/spicy-prancing-nebula.md`](../../README.md),
section "Push-model implementation".

ADR 0009's "Update 2026-05-07: implementation diverges from original
assumptions" section is now historical. Hybrid scrape never shipped beyond
the GitOps scaffolding in commit `bf2f68f`. This ADR supersedes the "v1
ships Option A" decision.
