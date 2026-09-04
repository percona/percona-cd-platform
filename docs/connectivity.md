# Connectivity

The network detail behind [`architecture.md`](architecture.md): address plan,
peering mesh, every request path hop by hop, security-group gates, egress,
and DNS ownership. Design rationale lives in
[ADR 0002](adr/0002-public-path-vs-privatelink.md) (public path over
PrivateLink) and
[ADR 0019](adr/0019-shared-alb-ssl-termination-for-jenkins-masters.md)
(shared-ALB TLS).

## Address plan

| VPC | Region | CIDR | Owner |
|---|---|---|---|
| `percona-ci-platform-vpc` (EKS hub) | us-east-1 | 10.220.0.0/16 | `terraform/vpc.tf` |
| `jenkins-ps80` | us-west-2 | 10.155.0.0/22 | `terraform/master-ps80.tf` |
| `jenkins-pxc` | us-west-1 | 10.156.0.0/22 | `terraform/master-pxc.tf` |
| `jenkins-ps57` | eu-central-1 | 10.157.0.0/22 | `terraform/master-ps57.tf` |
| `jenkins-pmm-amzn2` | us-east-2 | 10.166.0.0/22 | `terraform/master-pmm.tf` |
| `jenkins-cloud` | eu-west-1 | 10.177.0.0/22 | `terraform/master-cloud.tf` |
| `jenkins-pxb` | us-west-2 | 10.179.0.0/22 | `terraform/master-pxb.tf` |
| `jenkins-ps3` (worker substrate only) | eu-west-1 | 10.181.0.0/22 | `terraform/master-ps3.tf` |
| `jenkins-psmdb` | us-west-2 | 10.188.0.0/22 | `terraform/master-psmdb.tf` |
| `jenkins-rel` | eu-west-1 | 10.199.0.0/22 | `terraform/master-rel.tf` |
| `molecule-tests-ps80` | us-west-2 | 10.177.0.0/22 | `terraform/molecule-tests.tf` |
| `molecule-tests-pxc` | us-west-1 | 10.177.0.0/22 | `terraform/molecule-tests.tf` |
| `jenkins-pg` (imported live VPC, kept) | eu-central-1 | 10.144.0.0/22 (+ secondary 10.145.0.0/21) | `terraform/master-pg.tf` |

Every peered CIDR is unique fleet-wide. This is a standing migration
precondition (ps57 and pxc were re-CIDRed off 10.177 for it). The
molecule-tests VPCs reuse 10.177.0.0/22 deliberately: they are never peered
to anything, so no conflict arises. The hub keeps a reserved-ranges note in
`terraform/variables.tf` (`var.vpc_cidr` description).

Master VPC shape (from `terraform/modules/jenkins-master`): /24 subnets
carved from the /22 (AZ indexes 1 and 2 by default, with psmdb adding index 0),
all public with auto-assigned IPv4, one route table, an IGW default route,
and a same-region S3 gateway endpoint. No NAT gateways exist in any master
VPC. The EKS hub runs three AZs with private/public subnet split and a
single NAT gateway in us-east-1a.

## Peering mesh

Strict hub and spoke ([VPC peering](https://docs.aws.amazon.com/vpc/latest/peering/what-is-vpc-peering.html)), ten peerings, all defined one-per-master-file:

- Requester is always the hub (us-east-1). Accepter uses the master
  region's provider alias. Name tag
  `percona-ci-platform-to-jenkins-<host>`.
- Hub-side routes (`<master CIDR> -> pcx`) are added to the **private**
  route tables only, because the proxy and reconciler pods run on private-subnet
  nodes. No peering routes exist on public route tables.
- Master-side route is the VPC's single route table:
  `10.220.0.0/16 -> pcx`.
- No spoke-to-spoke peering exists, and peering is [non-transitive](https://docs.aws.amazon.com/vpc/latest/peering/invalid-peering-configurations.html). Masters cannot reach one another
  privately.
- Special cases: the ps3 peering targets its worker-substrate VPC (the
  in-cluster controller reaches Graviton workers through it). pmm has an
  additional same-region peering to `pmm-staging` (10.178.0.0/22) that is
  deliberately not Terraform-managed here (recorded in
  `terraform/master-pmm.tf` comments).

## Request paths

### Mode B web path (nine EC2 masters)

```
user
 -> <host>.cd.percona.com          Route53 record, published by external-dns
 -> jenkins-masters ALB :443       ACM wildcard via SNI, TLS 1.3 PQ policy,
                                   ssl-redirect, target-type ip
 -> nginx pod :80                  2x nginx:1.27-alpine (jenkins-ingress)
 -> Service jenkins-<host> :8080   ClusterIP, selectorless
 -> EndpointSlice IP               written by jenkins-endpoint-reconciler
 -> EC2 master :8080               over the VPC peering, plain HTTP
```

Mechanics worth knowing:

- The ALB health check targets nginx's always-200 `/-/healthy` on :8081,
  deliberately decoupling ALB target health from master state: a down
  master serves a styled auto-refresh 503 instead of failing the target
  group.
- nginx resolves the Service name through kube-dns with `valid=5s` and a
  variable upstream, so it re-resolves at runtime instead of pinning the
  IP at startup. It injects `Host: <host>.cd.percona.com`, X-Forwarded
  headers, WebSocket upgrade, and a 3600 s read timeout.
- The reconciler CronJob runs every minute (staleness bound). It discovers
  the master via `ec2:DescribeInstances` filtered on
  `tag:iit-billing-tag=<short_name>` + `running` in the master's region,
  then writes the
  [EndpointSlice](https://kubernetes.io/docs/concepts/services-networking/endpoint-slices/)
  behind the
  [selectorless Service](https://kubernetes.io/docs/concepts/services-networking/service/#services-without-selectors).
  Zero matches writes an empty slice (the proxy serves the 503 page).
  Every match, including a sole one, is TCP-probed on :8080 and the newest
  *serving* instance wins, so a still-booting master is never routed to
  before Jenkins listens. If none serve, the existing slice is kept so a
  ghost can never blackhole ingress. AWS access is an
  [EKS Pod Identity](https://docs.aws.amazon.com/eks/latest/userguide/pod-identities.html)
  role holding bare `ec2:DescribeInstances`. Cluster access is a
  namespace-scoped Role over EndpointSlices.
- The discovery tag scheme is uniform: the module sets `iit-billing-tag`
  to its `short_name` on every master. The filter value is
  `jenkins-<host>` everywhere except pmm, whose short_name stays
  `jenkins-pmm-amzn2`. The value is inherited from its CloudFormation
  stack name and kept deliberately, because the string also names the IAM
  roles, the instance profile, the init-config bucket, and the billing
  tag, and renaming live identities during a cutover buys nothing
  (`terraform/master-pmm.tf` records the decision).
- Master upstreams are discovered by the in-cluster
  jenkins-endpoint-reconciler, which writes each `jenkins-<host>` Service
  EndpointSlice from the live EC2 private IP.

### Mode A web path (ps3, in-cluster)

```
user -> ps3.cd.percona.com -> jenkins-masters ALB :443 -> controller pod :8080
```

Host-only Ingress from the chart (`resources/jenkins/master`), health check
`/login`, same wildcard cert. No proxy, no reconciler, no peering in the
path.

### Platform UIs (`jenkins-cd` ALB group)

| Host | Backend | Health check |
|---|---|---|
| `grafana.cd` | grafana :3000 | `/api/health` |
| `argo.cd` | argocd-server :8080 (plus a gRPC target group) | `/healthz` |
| `auth.cd` | authentik :9000 | `/-/health/live/` |
| `headlamp.cd` | headlamp | `/` |
| `mimir-push.cd`, `loki-push.cd`, `tempo-push.cd` | alloy-gateway | `/-/ready` |

UI logins go through Authentik (OIDC), which bridges to Duo via SAML
([`authentication.md`](authentication.md)). The Jenkins controllers
themselves still use per-instance GitHub OAuth realms.

### Observability push path

```
master Alloy (systemd)
 -> {mimir,loki,tempo}-push.cd.percona.com :443   jenkins-cd ALB
 -> nginx-auth sidecar :9009/:3100/:4318          validates Bearer, strips it
 -> gateway Alloy loopback receivers
 -> mimir-distributor :8080 | loki-distributor :3100 | tempo :4317 (OTLP)
```

The bearer token is fetched by each master at every Alloy restart from
Secrets Manager (`percona-ci-platform/alloy-gateway/bearer`) using the
master's instance role. The gateway side receives it through an
ExternalSecret with a 1 h refresh. The in-cluster ps3 controller bypasses
the gateway and writes to the Mimir distributor directly. Full design:
[ADR 0013](adr/0013-push-from-masters-with-nginx-bearer.md),
[`observability.md`](observability.md).

## Worker-plane connectivity

| Plane | Initiated by | Path | Gate |
|---|---|---|---|
| EC2 classic (ec2 plugin) | Master | SSH :22 to the worker's public DNS, worker in the master's own VPC | Worker SG (cloud.groovy netMap), host-key checking off |
| EC2 Fleet / Graviton ASG | Master | AutoScaling API with the master's instance profile, then SSH :22 to the worker's **private** IP (same VPC) | Worker SG allows :22 from the master VPC CIDR |
| Hetzner Cloud | Master | Hetzner API (`htz.cd.token`), then SSH as root :22 to the server's **public** address | Hetzner-side, master egress only |
| pxc inbound JNLP (exception) | Agent (corp DC) | TCP to the retained EIP :50000 | SG rule `40.143.89.204/30 -> :50000`, host pinned by `jnlpHost.groovy` |
| ps3-k8s Graviton | Controller pod | ASG API via Pod Identity, then SSH :22 to worker private IPs over the EKS-to-ps3 peering | Worker SG allows :22 from the EKS VPC CIDR |

Build and agent traffic never traverses the ALBs. An EKS-cell outage
degrades web access and observability, not running builds.

## Security groups

- Each EC2 master attaches three SGs: the VPC default (intra-VPC worker
  traffic), `HTTP` (ingress only from `extra_http_ingress`: :8080 from the
  EKS VPC CIDR over the peering, plus pxc's :50000 JNLP exception), and
  `SSH` (:22 from `ssh_allowed_cidrs`, a six-CIDR operator baseline, plus
  one extra on pmm).
- SSH identity is also code: each master's `ssh_key_engineers` list names
  the engineers whose public keys boot user-data fetches from
  percona.com into `ec2-user`'s authorized_keys. The list is per master in
  `terraform/master-<host>.tf` and changes take effect at instance
  replacement, so key removal is not immediate. Tool-driven access ([EC2
  Instance Connect](https://docs.aws.amazon.com/AWSEC2/latest/UserGuide/ec2-instance-connect-overview.html) style: a temporary :22 rule plus an ephemeral pushed
  key) is gated by AWS IAM rather than the static list.
- The CFN-era world-open :80/:443 ingress was removed from the module once
  every consumer moved behind the ALB: nothing on a Terraform master listens on
  those ports (TLS terminates at the ALB, the user-data installs no
  openresty or certbot).
- ARM worker SGs allow :22 from their own VPC CIDR (ps3's also from the
  EKS CIDR). Classic-plugin worker SGs are defined in each master's cloud
  config, not in Terraform.
- The molecule-tests SGs (:22 and ICMP from anywhere, by design: molecule
  workers SSH test VMs over public IPs) are adopted with the hard rule that
  Terraform must mirror the playbooks' `create.yml` exactly.

## Egress and endpoints

Two deliberate egress models coexist, chosen by traffic shape.

- **Master VPCs are NAT-less.** Masters and workers sit on public subnets
  and egress directly through the IGW with auto-assigned addresses. The
  reasoning is volume and exposure: build fleets pull packages, images,
  and sources at build volume, and a NAT gateway would add an hourly
  charge plus a per-GB processing fee on all of it, while inbound exposure
  is already controlled by security groups rather than subnet privacy.
  The CloudFormation era used the same shape, so the migration preserved
  it. S3 traffic (init-config fetch, build caches) bypasses the IGW
  entirely through each VPC's free S3 gateway endpoint.
- **The EKS hub is the opposite model.** Pods live on private subnets
  behind a single NAT gateway in us-east-1a. The single-AZ collapse
  (2026-05) was measured, not guessed: all stateful workloads were already
  pinned to 1a, the S3 gateway endpoint absorbs the bulk of egress (ECR
  layer pulls, Helm charts), and residual NAT traffic across the previous
  three gateways was about 0.4 MB over 30 days, so two NATs were dropped
  for roughly 64 USD/month idle savings. Interface endpoints for
  `ecr.api`, `ecr.dkr`, `sts`, and `ec2` keep control-plane chatter off
  the NAT. Further interface endpoints are deferred until the NAT bill
  warrants them (`docs/eks-hardening.md`, item 11).
- Shell access is [SSM Session Manager](https://docs.aws.amazon.com/systems-manager/latest/userguide/session-manager.html) (the agent dials out on :443 with no inbound
  required). Direct SSH exists only inside `ssh_allowed_cidrs`
  ([`runbooks/master-shell-access.md`](runbooks/master-shell-access.md)).

## DNS ownership

Three writers share the `cd.percona.com` zone, and the boundaries are
mechanical, not conventional.

- The zone itself is looked up, never created, by this repo.
- [external-dns](https://kubernetes-sigs.github.io/external-dns/latest/) owns every friendly hostname (sources Ingress + Service,
  `policy: sync`, scoped to the one zone). Ownership is enforced through
  its TXT registry: each managed record is paired with a TXT record
  stamped `txtOwnerId: percona-ci-platform`, and the sync policy only
  creates, updates, or deletes records carrying that stamp. Records it
  does not own, such as records predating the platform, are invisible to it
  and cannot be clobbered.
- Terraform owns only the ACM validation records. The jenkins-master module
  creates no public DNS records, so no master ever publishes its raw address.

## EIP policy

EIP-less is the default, for one structural reason: with the ALB owning
web access and SSM owning shell access, a stable public IPv4 serves no
remaining purpose on a master. The module skips address association and
the master rides its subnet-auto-assigned public IPv4, which may change on
any instance replacement. The operational rule that follows is absolute:
nothing may pin to a master's public address, and discovery is always
live (`just ssh` lists the current addresses). Dropping the four wave EIPs
also removed their hourly public-IPv4 charges, but the pinning hazard was
the real motive. The one exception is pxc, which keeps an EIP because an
inbound JNLP agent is pinned to it (see the worker-plane table).
