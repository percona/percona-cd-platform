# 0019 — Shared-ALB SSL termination for Jenkins masters

**Status:** Accepted (2026-05-18)
**Related:** [ADR 0003](0003-acm-vs-cert-manager-for-alb.md) (ACM strategy this builds on), [ADR 0013](0013-push-from-masters-with-nginx-bearer.md) (push-model pattern reused for the proxy nginx)
**Amendment (2026-05-18, ps3 cutover):** path between the proxy pod and the master is cross-region VPC peering, not a public NAT hairpin. See "Decision" Mode B and "Consequences" sections, both updated below; `terraform/peering-<host>.tf` and the per-host CIDR notes capture the operational shape.

## Context

Each of the 10 active EC2 Jenkins masters (`pmm`, `ps80`, `ps3`, `pxc`, `pxb`, `psmdb`, `pg`, `ps57`, `rel`, `cloud`) runs its own openresty + Let's Encrypt certbot stack to terminate TLS for `<host>.cd.percona.com`. The pattern was load-bearing through 2019-2026 but produces a recurring failure class:

1. Certbot HTTP-01 renewals fail when the master's `:80` is briefly unreachable (spot rotation, AMI patch, SG mistake). `Percona-Lab/jenkins-pipelines` PR #3866 made certbot non-fatal so a failed renewal no longer breaks the SpotFleet bootstrap, but the underlying mechanism is still fragile.
2. Openresty + certbot drag ~200 lines of bash through every master's CloudFormation user-data (`create_fake_ssl_cert`, `setup_nginx`, `setup_letsencrypt`, `setup_dhparam`, `setup_nginx_allow_list`, `restore_real_cert_from_backup`), one diff at a time across 10 templates whenever the pattern changes.
3. The fleet has 4 VPCs colliding on `10.177.0.0/22` (`pxc`/`ps57`/`cloud`/`ps3`), blocking any future TGW/VPC-peering plan as long as the masters keep their pet network identities.
4. The cluster already runs a shared `jenkins-cd` ALB serving platform-control-plane Ingresses (grafana, argocd, authentik, alloy-gateway), with the wildcard `*.cd.percona.com` ACM cert in `us-east-1` already discovered via SNI. Standing up centralised SSL termination for Jenkins reuses that infrastructure.

PS-10945 is the parent ticket. Four prior codex reviews on the same chat thread converged on the pattern below after evaluating CloudFront, VPC Lattice, per-region ALBs, and "do nothing + switch certbot to DNS-01."

## Decision

Mode B in-cluster reverse proxy on a **dedicated** ALB.

Architecture:

```
Internet
  -> ALB (us-east-1, group.name=jenkins-masters, wildcard ACM via SNI)
  -> jenkins-ingress nginx Pod (resources/addons/jenkins-ingress/)
  -> Cross-region VPC peering (terraform/peering-<host>.tf)
  -> origin-<host>.cd.percona.com (private IP on master ENI, terraform/origins.tf)
  -> Jenkins :8080 on the master (no openresty, no TLS)
```

Key choices:

- **Dedicated `jenkins-masters` ALB**, not the shared `jenkins-cd` ALB. Jenkins traffic gets its own load balancer so a master rule churn or proxy outage cannot cascade into the platform control-plane ingress. ALBC creates and owns the second ALB automatically.
- **One Helm chart, N hosts.** `resources/addons/jenkins-ingress/values.yaml` declares each master as `{ name, upstream, upstreamScheme, upstreamPort, upstreamHostHeader }`. Rolling a master in or out of the proxy is a values.yaml edit; ArgoCD syncs the addon in seconds.
- **IngressGroup pattern.** `alb.ingress.kubernetes.io/group.name: jenkins-masters` lets ALBC merge every per-host Ingress onto the same ALB with host-header routing. One ALB, N rules, well under the 100-rule/listener quota.
- **`origin-<host>` upstream convention** (`terraform/origins.tf`, `var.jenkins_origin_targets` in gitignored `terraform/local.auto.tfvars`). The proxy connects to a stable per-master DNS name that resolves to the master's **private** IP on the master ENI, reached via the cross-region peering. Decoupled from the public `<host>.cd.percona.com` Route53 record so cutting the public hostname over to the ALB does not loop nginx back into itself.
- **Cross-region VPC peering** (`terraform/peering-<host>.tf`) gives the proxy pod a private path to the master, avoiding the public-internet hairpin and the master-side `:8080` `0.0.0.0/0` SG hole. EKS VPC is `10.220.0.0/16`; each master VPC must use a non-overlapping CIDR (ps3 was renumbered `10.177.0.0/22` -> `10.181.0.0/22` for this; the `pxc`/`ps57`/`cloud` collidors stay on `10.177.0.0/22` until each is peered in turn, since peering a single collidor at a time is unambiguous).
- **Runtime DNS resolution + connect-retry** in the nginx config. `resolver kube-dns.kube-system.svc.cluster.local valid=5s;` plus `$upstream` variable forces nginx to re-resolve on every request, so an EIP swap propagates within ~5s without a pod restart. `proxy_connect_timeout 5s` + `proxy_next_upstream` + `error_page 502 503 504 -> @backend_down` (auto-refresh 15s) turns a 2-5 min spot-rotation window into a friendly degraded-state HTML page instead of raw connection-refused errors.
- **external-dns owns the public `<host>.cd.percona.com` Route53 records.** Per-master CloudFormation deletes its previous `AWS::Route53::RecordSet JDNSRecord` so external-dns (running `--policy=sync --registry=txt --txt-owner-id=percona-ci-platform`) can publish a fresh A-alias to the ALB. JHostName parameter stays in the CF template (still drives `JENKINS_HOST` in user-data).
- **Master listens on `:8080` only at target state.** The ALB terminates TLS; the master no longer needs openresty. Per-master `update-stack` strips `setup_nginx` / `setup_letsencrypt` / `create_fake_ssl_cert` / `setup_dhparam` / `setup_nginx_allow_list` / `restore_real_cert_from_backup` from user-data, drops `openresty` and `certbot` from the yum install, and the same stack-update tightens `HTTPSecurityGroup` to allow `tcp/8080` only from the EKS VPC CIDR (`10.220.0.0/16`) over the peering. `:80`/`:443` close to `0.0.0.0/0`; SSH stays on the existing /32 allowlist (EC2 Instance Connect Endpoint is preferred for operator access).

### Why an nginx pod, not direct ALB IP targets?

A natural simplification is "skip the nginx pod, register the master's private IP directly as an ALB target." Three load-bearing reasons it doesn't work / isn't worth it:

1. **ALB IP targets are same-region only.** The ALB lives in `us-east-1`; the 10 masters span 5 other regions. Cross-region peering does not change this constraint — `aws elbv2 register-targets` rejects an IP outside the ALB's region even when the route to it exists. The nginx pod gives the ALB a same-region target (it's in the EKS VPC) that then forwards cross-region over the peering.
2. **Dynamic upstream re-resolution.** nginx uses `resolver` + a `$upstream` variable so it re-resolves `origin-<host>` DNS every 5s. When the master's IP changes (renumber, AZ failover, SpotFleet replacement with a fresh ENI), the proxy keeps working with zero infra change. An ALB target group needs explicit (re-)registration; the IP change becomes a coordination problem.
3. **Resilience layer the ALB can't provide.** Custom 503 auto-refresh page during master outages, WebSocket upgrade headers for Jenkins console streaming, `proxy_next_upstream` retries inside the 5s connect timeout. ALB -> Jenkins direct would surface raw 502s during the 2-5 min spot rotation window and would lose console streams.

Nice-to-have on top: with the nginx pod, N hosts collapse to **one** ALB target group (the nginx Service). Without it, each master would need its own ALB target group + per-master health-check config.

The cutover sequence per master is captured in `docs/runbooks/jenkins-ssl-cutover.md`. ps3 was the first cut over; the same 7-step procedure runs verbatim for the remaining 9 masters.

## Alternatives considered and rejected

- **VPC Lattice service network.** Lattice is regional in 2026 (https://aws.amazon.com/vpc/lattice/faqs/); a `us-east-1` service network cannot directly hold target groups in the 5 other regions where masters live. Per-region service networks plus cross-region endpoints multiply moving parts for no clear win over an in-cluster reverse proxy.
- **TGW peering + IP targets.** TGW is a fleet-wide cross-region hub-and-spoke alternative to per-master peering. Rejected for now because (a) the 10 masters live in only 4 regions (eu-west-1, eu-central-1, us-east-2, us-west-1, us-west-2) and per-master peerings are ~30 LoC each, (b) TGW adds ~$36/mo per attachment plus data-transfer charges that compound vs the per-peering model, (c) the 4-way `10.177.0.0/22` VPC collision still has to be unwound either way. Revisit once 5+ peerings are live and the TF surface area passes the per-master pattern.
- **CloudFront + public origins + shared ACM.** Viable and simpler than Lattice. Rejected because (a) CloudFront's caching semantics for Jenkins web/API traffic add complexity for negligible benefit, (b) Egress from CloudFront to AWS origins is free but request fan-out cost grows with build-artifact downloads, (c) Operating two parallel ingress planes (CloudFront for Jenkins, ALB for platform) doubles the failure-injection surface.
- **DNS-01 certbot via Route53.** Cheapest, no infrastructure change. Rejected because it leaves all 10 masters as pets each running their own SSL stack; doesn't progress the "Jenkins on the platform EKS" vision.
- **Per-region EKS + ALB + IngressGroup.** Would need EKS in 5 regions (current platform is single-region `us-east-1`). Operating surface multiplies by 5; cost of running the additional clusters dwarfs the egress hairpin tax of routing through `us-east-1`.
- **Shared `jenkins-cd` ALB (instead of dedicated `jenkins-masters`).** Cheapest variant of the chosen pattern; one fewer ALB. Rejected because per-master Ingress reconciliation, certificate fallback, and rule-rate-limit pressure should not share fate with the platform control plane.
- **PrivateLink / cross-region VPC interface endpoints (drop the nginx pod).** Would put an NLB in front of each master :8080 in its own region, expose it as a VPC endpoint service, and consume it via an interface endpoint in the EKS VPC. Cross-region PrivateLink has been supported since 2022, and the interface endpoint ENIs *are* same-region as the ALB so the IP-target constraint goes away. Rejected because (a) per-master cost is ~$22/mo for the interface endpoint ENIs alone (one per AZ at $0.01/hr), before per-GB data processing on both sides; per-peering is free, (b) per-master resource count jumps from ~5 lines of peering routes to NLB + endpoint service + interface endpoint per AZ (~3-5 new resources), (c) the resilience layer (503 page, WebSocket upgrade, dynamic re-resolve) still has to come from *somewhere*, so the nginx pod doesn't actually go away unless we also accept ALB raw 5xx during outages.

## Consequences

- All 10 master web UIs share a fate domain with `percona-ci-platform` EKS. An ArgoCD / ALBC / EKS incident takes the Jenkins UIs offline. Build workers continue (they connect outbound and never traverse the ALB). Jenkins keeps its native GitHub OAuth SecurityRealm (users sign in via GitHub passkey / WebAuthn); Authentik is not in this path, only in Grafana's. An Authentik outage hits Grafana only, never Jenkins.
- Hairpin latency: EU clients hitting an EU master traverse `us-east-1`. ~80 ms added per request. Mitigation if user pain is real: per-region EKS+ALB, separate ADR.
- Egress doubling: ALL traffic via ALB doubles egress cost vs direct master. Keep large artifact downloads on direct master URLs (separate `<host>-artifacts.cd.percona.com` -> master) once the cutover is proven, separate cleanup.
- Spot rotation surfaces as a 503 + auto-refresh page on the proxy (5 min worst case), instead of raw connection-refused errors. EBS volume + EIP survive spot rotation untouched, so JENKINS_HOME and the public-facing hostname stay stable across replacement.
- Per-master cutover is a 1-PR-per-side + 1-tfvars-edit + 1-runbook-walk pattern (percona-ci-platform `terraform/peering-<host>.tf` + `local.auto.tfvars` origin target; `Percona-Lab/jenkins-pipelines` single PR that renumbers CIDR if needed, strips openresty/certbot, tightens SG to `10.220.0.0/16:8080`, drops CF JDNSRecord). Blast radius is contained to one master at a time.
- Long-standing `10.177.0.0/22` VPC collision is no longer an SSL blocker but DOES bite per-master peering: two collidors cannot both peer to EKS at the same time. ps3 was renumbered to `10.181.0.0/22` as part of this cutover; the next collidor to be wired in (`pxc`/`ps57`/`cloud`) needs a fresh /22. Per-master peerings stay clean as long as each master VPC's CIDR is unique against EKS (`10.220.0.0/16`) AND against any other already-peered master.
- `jenkins_origin_targets` in `local.auto.tfvars` is currently a hand-edited per-master private IP. It SHOULD be a filtered data source (`data.aws_instances` tag-filtered by `iit-billing-tag=jenkins-<host>`) so the master's IP is auto-discovered, matching the pattern already used for VPC + route-table lookups in `peering-<host>.tf`. Trade-off: `tofu apply` becomes load-bearing for IP refresh (a SpotFleet replacement window with no running master would return zero results and need a `try(..., fallback)` wrapper). Refactor is a separate follow-up; the manual tfvars is unblocked for now.

## References

- PS-10945 spike plan: `~/.claude/plans/validated-herding-stardust.md`
- Per-master cutover procedure: `docs/runbooks/jenkins-ssl-cutover.md`
- Addon: `resources/addons/jenkins-ingress/`
- Per-master IaC: `Percona-Lab/jenkins-pipelines/IaC/<host>.cd/JenkinsStack.yml`
- Origin record machinery: `terraform/origins.tf` + `terraform/variables.tf` (`jenkins_hosts`, `jenkins_origin_targets`)
