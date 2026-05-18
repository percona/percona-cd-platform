# 0019 — Shared-ALB SSL termination for Jenkins masters

**Status:** Accepted (2026-05-18)
**Related:** [ADR 0003](0003-acm-vs-cert-manager-for-alb.md) (ACM strategy this builds on), [ADR 0013](0013-push-from-masters-with-nginx-bearer.md) (push-model pattern reused for the proxy nginx)

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
  -> Internet -> origin-<host>.cd.percona.com (master EIP, terraform/origins.tf)
  -> openresty (transitional) OR Jenkins :8080 direct (target state)
```

Key choices:

- **Dedicated `jenkins-masters` ALB**, not the shared `jenkins-cd` ALB. Jenkins traffic gets its own load balancer so a master rule churn or proxy outage cannot cascade into the platform control-plane ingress. ALBC creates and owns the second ALB automatically.
- **One Helm chart, N hosts.** `resources/addons/jenkins-ingress/values.yaml` declares each master as `{ name, upstream, upstreamScheme, upstreamPort, upstreamHostHeader }`. Rolling a master in or out of the proxy is a values.yaml edit; ArgoCD syncs the addon in seconds.
- **IngressGroup pattern.** `alb.ingress.kubernetes.io/group.name: jenkins-masters` lets ALBC merge every per-host Ingress onto the same ALB with host-header routing. One ALB, N rules, well under the 100-rule/listener quota.
- **`origin-<host>` upstream convention** (`terraform/origins.tf`, `var.jenkins_origin_targets` in gitignored `terraform/local.auto.tfvars`). The proxy connects to a stable per-master DNS name that resolves to the master's EIP, decoupled from the public `<host>.cd.percona.com` Route53 record. Cutting the public hostname over to the ALB does not loop nginx back into itself.
- **Runtime DNS resolution + connect-retry** in the nginx config. `resolver kube-dns.kube-system.svc.cluster.local valid=5s;` plus `$upstream` variable forces nginx to re-resolve on every request, so an EIP swap propagates within ~5s without a pod restart. `proxy_connect_timeout 5s` + `proxy_next_upstream` + `error_page 502 503 504 -> @backend_down` (auto-refresh 15s) turns a 2-5 min spot-rotation window into a friendly degraded-state HTML page instead of raw connection-refused errors.
- **external-dns owns the public `<host>.cd.percona.com` Route53 records.** Per-master CloudFormation deletes its previous `AWS::Route53::RecordSet JDNSRecord` so external-dns (running `--policy=sync --registry=txt --txt-owner-id=percona-ci-platform`) can publish a fresh A-alias to the ALB. JHostName parameter stays in the CF template (still drives `JENKINS_HOST` in user-data).
- **Master listens on `:8080` only at target state.** The shared ALB terminates TLS; the master no longer needs openresty. Per-master `update-stack` strips `setup_nginx` / `setup_letsencrypt` / `create_fake_ssl_cert` / `setup_dhparam` / `setup_nginx_allow_list` / `restore_real_cert_from_backup` from user-data, drops `openresty` and `certbot` from the yum install, and a follow-up SG PR closes `0.0.0.0/0 :443/:80` leaving only `tcp/8080` from the percona-ci-platform NAT egress EIP.

The cutover sequence per master is captured in `docs/runbooks/jenkins-ssl-cutover.md`. ps3 was the first cut over; the same 7-step procedure runs verbatim for the remaining 9 masters.

## Alternatives considered and rejected

- **VPC Lattice service network.** Lattice is regional in 2026 (https://aws.amazon.com/vpc/lattice/faqs/); a `us-east-1` service network cannot directly hold target groups in the 5 other regions where masters live. Per-region service networks plus cross-region endpoints multiply moving parts for no clear win over an in-cluster reverse proxy.
- **TGW peering + IP targets.** Cleanest L3 model on paper, but the 4-way `10.177.0.0/22` VPC collision blocks it until those VPCs renumber. EBS-anchored VPC renumber is a separate ~30-45 min outage per master.
- **CloudFront + public origins + shared ACM.** Viable and simpler than Lattice. Rejected because (a) CloudFront's caching semantics for Jenkins web/API traffic add complexity for negligible benefit, (b) Egress from CloudFront to AWS origins is free but request fan-out cost grows with build-artifact downloads, (c) Operating two parallel ingress planes (CloudFront for Jenkins, ALB for platform) doubles the failure-injection surface.
- **DNS-01 certbot via Route53.** Cheapest, no infrastructure change. Rejected because it leaves all 10 masters as pets each running their own SSL stack; doesn't progress the "Jenkins on the platform EKS" vision.
- **Per-region EKS + ALB + IngressGroup.** Would need EKS in 5 regions (current platform is single-region `us-east-1`). Operating surface multiplies by 5; cost of running the additional clusters dwarfs the egress hairpin tax of routing through `us-east-1`.
- **Shared `jenkins-cd` ALB (instead of dedicated `jenkins-masters`).** Cheapest variant of the chosen pattern; one fewer ALB. Rejected because per-master Ingress reconciliation, certificate fallback, and rule-rate-limit pressure should not share fate with the platform control plane.

## Consequences

- All 10 master web UIs share a fate domain with `percona-ci-platform` EKS. An ArgoCD / ALBC / EKS incident takes the Jenkins UIs offline. Build workers continue (they connect outbound and never traverse the ALB). Jenkins keeps its native GitHub OAuth SecurityRealm (users sign in via GitHub passkey / WebAuthn); Authentik is not in this path, only in Grafana's. An Authentik outage hits Grafana only, never Jenkins.
- Hairpin latency: EU clients hitting an EU master traverse `us-east-1`. ~80 ms added per request. Mitigation if user pain is real: per-region EKS+ALB, separate ADR.
- Egress doubling: ALL traffic via ALB doubles egress cost vs direct master. Keep large artifact downloads on direct master URLs (separate `<host>-artifacts.cd.percona.com` -> master) once the cutover is proven, separate cleanup.
- Spot rotation surfaces as a 503 + auto-refresh page on the proxy (5 min worst case), instead of raw connection-refused errors. EBS volume + EIP survive spot rotation untouched, so JENKINS_HOME and the public-facing hostname stay stable across replacement.
- Per-master cutover is a 3-PR + 1-tfvars-edit + 1-runbook-walk pattern (`Percona-Lab/jenkins-pipelines` SG-PR + strip-openresty PR + remove-CF-DNS PR; `terraform/local.auto.tfvars` origin target). Blast radius is contained to one master at a time.
- Long-standing `10.177.0.0/22` VPC collision is now an SSL non-issue: the proxy hops over the public internet to each master EIP, so private CIDR overlap stays irrelevant. The collision remains a blocker for any future TGW plan but is no longer a blocker for centralised TLS.

## References

- PS-10945 spike plan: `~/.claude/plans/validated-herding-stardust.md`
- Per-master cutover procedure: `docs/runbooks/jenkins-ssl-cutover.md`
- Addon: `resources/addons/jenkins-ingress/`
- Per-master IaC: `Percona-Lab/jenkins-pipelines/IaC/<host>.cd/JenkinsStack.yml`
- Origin record machinery: `terraform/origins.tf` + `terraform/variables.tf` (`jenkins_hosts`, `jenkins_origin_targets`)
