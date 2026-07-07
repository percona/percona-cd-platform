# Break-glass DNS runbook (Jenkins master web plane)

See [`../architecture.md`](../architecture.md) and
[ADR 0019](../adr/0019-shared-alb-ssl-termination-for-jenkins-masters.md) for the
normal path. This runbook is the emergency bypass when that path is gone.

## Normal path (for context)

`<host>.cd.percona.com` -> `jenkins-masters` ALB (us-east-1) -> in-cluster
`jenkins-ingress` NGINX (3 replicas, one per node, across us-east-1a/b/c) ->
cross-region VPC peering -> master `:8080`. external-dns owns the
`<host>.cd.percona.com` records (`--policy=sync`, `--txt-owner-id=percona-ci-platform`).

## When to use this

A whole-cell or whole-region front-door loss: the us-east-1 EKS cell, the
`jenkins-masters` ALB, or the `jenkins-ingress` NGINX is down AND you need
emergency UI/API access to one or more master controllers.

Do NOT use it for smaller failures, which now self-heal:

- A single us-east-1a AZ loss or a single node loss no longer takes the web
  plane down. Since the 2026-06-15 de-pin the NGINX runs 3 replicas, one per
  node, across all three AZs (podAntiAffinity + zone spread), so at most one
  replica is lost and the proxy keeps serving.
- Build workers never traverse the ALB (they connect outbound), so builds and
  worker provisioning continue through a front-door outage regardless.
- `ps3` runs in-cluster, so a cell outage stops it entirely. There is no DNS
  break-glass for ps3; its recovery is cell recovery
  ([disaster-recovery.md](disaster-recovery.md)).

## Procedure (per affected master)

1. **Confirm the master itself is healthy** (only the front door is down). The
   controller EC2 instance must be `running`:

   ```sh
   paws ec2 -i <host>          # or: aws ec2 describe-instances \
   #   --filters Name=tag:iit-billing-tag,Values=jenkins-<host> \
   #             Name=instance-state-name,Values=running
   ```

2. **Get the master's current public IP.** TF masters are EIP-less (dynamic
   public IP), so read it live, never from memory:

   ```sh
   aws ec2 describe-instances \
     --filters Name=tag:iit-billing-tag,Values=jenkins-<host> \
               Name=instance-state-name,Values=running \
     --query 'Reservations[].Instances[].PublicIpAddress' --output text
   ```

   `pxc` keeps an EIP (stable address); every other master, pg included, is EIP-less.

3. **Open the master security group for operator access on `:8080`.** TF masters
   serve plain `:8080` (openresty/TLS was stripped at cutover); the module SG
   admits only `extra_http_ingress` (`:8080` from the EKS CIDR). Two options:

   - Emergency (manual, accepts drift, fastest): add a temporary SG ingress rule
     `{tcp/8080, <operator CIDR>/32}` to the master SG in the console or CLI.
     Note the rule so it can be removed in recovery.
   - Codified (no drift, slower): add an `extra_http_ingress` entry
     `{ port = 8080, cidr = "<operator CIDR>/32" }` to the master's
     `module "jenkins-master"` call in `terraform/master-<host>.tf` and
     `tofu apply`. Prefer this once the immediate fire is out.

4. **Publish a break-glass DNS record external-dns will not fight.** external-dns
   owns `<host>.cd.percona.com` (txt-owner `percona-ci-platform`), so use a
   DIFFERENT name it does not manage. In the `cd.percona.com` hosted zone, create
   an A record `<host>-bg.cd.percona.com` -> the master public IP from step 2.
   external-dns leaves unowned records alone.

5. **Access the master.** TF masters: `http://<host>-bg.cd.percona.com:8080`
   (HTTP only, no TLS in break-glass; treat as operator-only emergency access).
   Login is unchanged. masters keep native GitHub OAuth, and Authentik is not in
   the Jenkins path, so authentication works independent of the cell.

   If HTTPS is mandatory for a TF master, stand up a temporary same-region
   nginx/ALB in front of it, or restore openresty. That is out of scope for a
   fast break-glass and rarely worth it for an operator-only window.

## Recovery (cell / ALB restored)

1. Delete every `<host>-bg.cd.percona.com` record created in step 4.
2. Remove the temporary SG ingress from step 3 (or revert the
   `extra_http_ingress` edit and `tofu apply`). Re-verify the SG admits only
   `:8080` from the EKS CIDR `10.220.0.0/16` (plus pxc `:50000`).
3. Confirm `<host>.cd.percona.com` resolves to the ALB again (external-dns
   re-owns it) and returns 200 through the proxy:
   `curl -sI https://<host>.cd.percona.com/-/healthy`.

## Limitations

- Break-glass is HTTP for TF masters (no TLS). Open it narrowly to an operator
  CIDR and close it promptly in recovery.
- No break-glass for `ps3` (in-cluster); see [disaster-recovery.md](disaster-recovery.md).
- This is for a whole-cell / whole-region loss only. AZ-level and node-level
  front-door failures self-heal after the 2026-06-15 web-plane de-pin.
