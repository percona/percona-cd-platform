# Cutover a Jenkins master to shared-ALB SSL termination

End state: an EC2 Jenkins master (`<host>.cd.percona.com`) is reached via
the dedicated `jenkins-masters` ALB in this cluster, terminating TLS with
the wildcard ACM cert. The master's openresty + certbot stack is gone;
Jenkins listens on `:8080` only; master SG allows `:8080` only from this
cluster's NAT egress EIP. (PS-10945)

This runbook is generic: substitute `<host>` for each of the 10 active
masters (`pmm`, `ps80`, `ps3`, `pxc`, `pxb`, `psmdb`, `pg`, `ps57`,
`rel`, `cloud`). Concrete examples use `ps3.cd.percona.com` because it
was the first cut over.

Out of scope:
- The `ps3-k8s` migration (Jenkins-as-pods in the cluster) is a separate
  track; see `docs/runbooks/migrate-ps3-to-eks.md`. Do not confuse the
  two: `ps3` here is the live EC2 master, not the in-cluster replica.
- The upstream `origin-<host>` Route53 record and the `jenkins_hosts`
  TF entry are prerequisites handled outside this runbook (operator
  populates them ahead of the first cutover; see comments in
  `terraform/variables.tf` and `terraform/origins.tf`).

## Prerequisites

- `jenkins-ingress` addon deployed (`kubectl get app -n argocd jenkins-ingress` reports `Synced Healthy`).
- Dedicated `jenkins-masters` ALB live (`k8s-jenkinsmasters-049b9e6405`).
- Operator has `percona-dev-admin` AWS credentials.
- Operator can `kubectl` against `percona-ci-platform`.
- Operator can SSH to the master via the existing /32 ingress.
- `<host>` is registered in `var.jenkins_hosts` and has its upstream
  Route53 record populated (see "Out of scope" above).

## Per-host cutover sequence

Walk these steps for one host at a time. Do not batch across masters
until the pattern is proven over 30+ days.

### 1. Smoke-test proxy reachability via the new ALB

`Host`-header to the dedicated ALB while production DNS still points at
the master:

```sh
ALB_DNS=k8s-jenkinsmasters-049b9e6405-2095602194.us-east-1.elb.amazonaws.com
ALB_IP=$(dig +short $ALB_DNS | head -1)
curl -sS -i --resolve ps3.cd.percona.com:443:$ALB_IP \
  https://ps3.cd.percona.com/login | head -20
```

Expect `HTTP/2 200` plus `x-jenkins:` and `x-hudson:` headers in the
response (proves the proxy is hitting the actual Jenkins backend, not
the friendly degraded-state page).

### 2. Open `:8080` on the master SG

Land the per-master SG PR in `Percona-Lab/jenkins-pipelines` that adds
`tcp/8080` ingress to `HTTPSecurityGroup` from
`54.156.234.228/32` (the percona-ci-platform NAT GW EIP). Keep `:443`
and `:80` open in this PR so the current openresty path stays usable.
ps3 example: PR #4087.

```sh
AWS_PROFILE=percona-dev-admin aws cloudformation update-stack \
  --region <region> --stack-name jenkins-<host> \
  --template-body file://IaC/<host>.cd/JenkinsStack.yml \
  --capabilities CAPABILITY_NAMED_IAM
```

Verify the live SG has the new rule:

```sh
AWS_PROFILE=percona-dev-admin aws ec2 describe-security-groups \
  --region <region> \
  --filters Name=vpc-id,Values=<vpc-id> Name=group-name,Values=HTTP \
  --query 'SecurityGroups[0].IpPermissions[?FromPort==`8080`]'
```

### 3. Flip the proxy upstream to `:8080`

In `resources/addons/jenkins-ingress/values.yaml`, change the host
entry to plain HTTP on `:8080`:

```yaml
- name: ps3
  upstream: <the host's upstream target, unchanged>
  upstreamScheme: http
  upstreamPort: 8080
  # upstreamHostHeader no longer needed (no SNI), but harmless to leave
```

Commit + push to `main`. ArgoCD syncs in ~1 min. nginx rolls (the
`checksum/config` annotation on the Deployment triggers a rollout when
the ConfigMap changes).

Re-run the curl from step 1; expect the same `HTTP/2 200` with Jenkins
headers, now via plain HTTP `:8080` on the master.

### 4. Land the strip-openresty PR + update-stack

Per-master PR strips `setup_nginx` / `setup_letsencrypt` /
`create_fake_ssl_cert` / `setup_dhparam` / `setup_nginx_allow_list` /
`restore_real_cert_from_backup` from the master's user-data, drops
`certbot` + `openresty` packages, removes the openresty yum repo.
ps3 example: PR #4085.

After CODEOWNERS approval + merge, `update-stack` cycles the SpotFleet
on the next deploy. Force a replacement to redeploy the server now
rather than waiting:

```sh
AWS_PROFILE=percona-dev-admin aws cloudformation update-stack \
  --region <region> --stack-name jenkins-<host> \
  --template-body file://IaC/<host>.cd/JenkinsStack.yml \
  --capabilities CAPABILITY_NAMED_IAM

# After UPDATE_COMPLETE, terminate the running master to force the
# SpotFleet to launch a fresh instance with the new user-data:
INSTANCE_ID=$(AWS_PROFILE=percona-dev-admin aws ec2 describe-instances \
  --region <region> \
  --filters Name=vpc-id,Values=<vpc-id> Name=instance-state-name,Values=running \
  --query 'Reservations[0].Instances[0].InstanceId' --output text)
AWS_PROFILE=percona-dev-admin aws ec2 terminate-instances \
  --region <region> --instance-ids $INSTANCE_ID
```

During the SpotFleet relaunch window (2-5 min) the proxy serves the
friendly degraded-state HTML (Phase 6 resilience: meta-refresh every
15s). Verify with `curl -i https://<host>.cd.percona.com/login` via
`--resolve` to the ALB.

When the new instance is up, `setup_aws` re-associates the master EIP
(unchanged), EBS re-attaches (unchanged), Jenkins starts on `:8080`
only. No openresty, no certbot.

### 5. One-time EBS cleanup (preserve JENKINS_HOME, remove SSL leftovers)

The EBS data volume `/mnt/<host>.cd.percona.com/` persists across
SpotFleet rotation. After step 4, `/mnt/<host>.cd.percona.com/ssl/`
and `/mnt/ssl_backup/` contain dead cert/key/dhparam files. The
strip-openresty user-data does not delete them (intentional: stay
minimal; one-shot cleanup happens here, not on every boot).

SSH to the master and remove (ps3 example):

```sh
ssh ps3.cd.percona.com 'sudo rm -rf \
  /mnt/ps3.cd.percona.com/ssl/ \
  /mnt/ssl_backup/'
```

Verify JENKINS_HOME is intact:

```sh
ssh ps3.cd.percona.com 'ls /mnt/ps3.cd.percona.com/ | head -20'
# Expect: config.xml, jobs/, plugins/, secrets/, users/, etc.
# Confirm absent: ssl/
```

### 6. Land the remove-CF-DNS PR + update-stack

Per-master PR deletes `AWS::Route53::RecordSet JDNSRecord` from the
template, releasing `<host>.cd.percona.com` for external-dns to publish
to the ALB. ps3 example: PR #4086.

After CODEOWNERS approval + merge:

```sh
AWS_PROFILE=percona-dev-admin aws cloudformation update-stack \
  --region <region> --stack-name jenkins-<host> \
  --template-body file://IaC/<host>.cd/JenkinsStack.yml \
  --capabilities CAPABILITY_NAMED_IAM
```

CF removes the live A record. Within ~1 minute external-dns publishes
the new A-alias pointing at the `jenkins-masters` ALB. Verify (ps3
example):

```sh
dig +short ps3.cd.percona.com
# Expect: ALB DNS CNAME chain, no longer the master EIP.
```

Open `https://ps3.cd.percona.com/login` in a browser. Production
traffic now flows ALB -> nginx pod -> Jenkins :8080 on the master.

### 7. SG cleanup (lock down master to NAT-only on :8080)

Final per-master PR removes `0.0.0.0/0` on `:443` and `:80` from
`HTTPSecurityGroup`, leaving only `54.156.234.228/32 :8080` (and the
unchanged SSH /32 allowlist on `SSHSecurityGroup`).

```sh
AWS_PROFILE=percona-dev-admin aws cloudformation update-stack \
  --region <region> --stack-name jenkins-<host> \
  --template-body file://IaC/<host>.cd/JenkinsStack.yml \
  --capabilities CAPABILITY_NAMED_IAM
```

Verify direct access is closed:

```sh
curl -k -m 5 https://<master EIP>/ 2>&1 | head -5
# Expect: connection refused or timeout.
```

## Rollback

Per step, reverse order:

| Step | Rollback |
|---|---|
| 7 | Restore `0.0.0.0/0 :443` rule via stack-update; sub-minute. |
| 6 | `aws route53 change-resource-record-sets` to restore A record at master EIP; sub-minute (TTL 60). |
| 5 | Irrelevant: cleanup is irreversible but harmless data loss (cert files). Re-issue via certbot if openresty is also rolled back. |
| 4 | Revert user-data strip + `update-stack`; SpotFleet cycles, openresty + certbot re-install. ~10 min. |
| 3 | Revert `jenkins-ingress` values.yaml in `main`; ArgoCD syncs in ~1 min. Upstream returns to `https://...:443`. |
| 2 | Leave SG rule in place (additive, no rollback needed unless cleaning audit). |
| 1 | N/A (verification only). |

## Known follow-ups

- ALB target-group health probes hit `/-/healthy` on the proxy pods,
  not the master. Master outage stays invisible to the ALB; alert on
  nginx 5xx rate via Loki instead.
- Hairpin latency: EU clients hitting an EU master traverse `us-east-1`.
  ~80 ms added per request. Mitigation if user pain is real: per-region
  EKS+ALB. Not in scope for the initial fleet rollout.
- EBS leftovers are cleaned once per master (step 5). If a master is
  ever re-imaged from scratch (snapshot restore), repeat step 5.
