# Common operations

Day-to-day changes in frequency order. Start at the table, jump to the
section. Golden rule: config changes never replace instances, and
anything that replaces an instance is announced in #opensource-jenkins
first (builds in flight die).

| Change | Edit | Impact | Run |
|--------|------|--------|-----|
| Worker template (label, AMI, type, init script) | `resources/jenkins-masters/<inst>/init.groovy.d/` | none, config only | `just runbook template-change <inst>` |
| Worker template on ps3-k8s | `resources/jenkins/clouds-catalog/` | none, JCasC hot-reload | `argocd app sync jenkins-ps3-k8s` |
| Resize or retype a master | `terraform/master-<inst>.tf` | **replaces the instance** | `just runbook master-resize` |
| Bump Jenkins core | `terraform/master-<inst>.tf` | **replaces the instance** | `just runbook core-bump` |
| Engineer SSH key | `terraform/master-<inst>.tf` | at next replacement | `just runbook ssh-key` |
| Port or SSH allow-list | `terraform/master-<inst>.tf` | in place | `just tf-plan && just tf-apply` |
| Graviton fleet size or types | `terraform/master-<inst>.tf` | in place | `just tf-plan && just tf-apply` |

`just runbook` lists the subcommands. `template-change` enforces the
gates below mechanically, the others confirm each step.

## Worker template on an EC2 master

The most common change by far.

1. Edit under `resources/jenkins-masters/<inst>/init.groovy.d/`:
   - `cloud.groovy`: EC2 templates (labels, AMIs, types, init script).
     On `cloud` the file is `cloud.groovy.tftpl`, keep `${...}` intact.
   - `htz.cloud.groovy`: Hetzner templates.
   - `ec2FleetCloud.groovy`: Graviton fleet wiring.
   - `matrix.groovy`: matrix-job label axes.
2. PR, merge.
3. `just runbook template-change <inst> [--file F]`. It requires a clean
   checkout of origin/main, plans, refuses any change beyond that
   master's init-config S3 objects, applies, and evaluates the file on
   the live JVM. No instance resources change.
4. Verify: probe build on the changed label, or template inventory via
   the jenkins CLI.

How the change propagates:

- S3 is canonical. The apply updates the bucket, nothing else.
- The live evaluate in step 3 is what makes it active now. Skipped, the
  change waits for the next instance replacement (boot re-fetches S3 to
  disk).
- A plain JVM restart re-runs the disk copy, which is stale until a
  replacement or a resync. Only masters that set
  `init_groovy_sync_schedule` get a periodic S3-to-disk resync
  (currently `cloud`, every 30 minutes).

Probe build parked on "doesn't have label": the label is fine, the
worker cannot launch. The EC2 plugin reacts to demand within seconds,
so check `aws ec2 describe-spot-instance-requests` in the master's
region next. `price-too-low` means the template's spot bid is under the
current market and the request waits forever (no on-demand fallback
unless the template enables it). Probe with a label whose template has
spot headroom.

## Worker template on the in-cluster master

ps3-k8s has no init.groovy.d. Edit the catalog under
`resources/jenkins/clouds-catalog/` (overlay `masters/ps3.yaml`), run
`just clouds-render-check`, merge, then sync inside a window:
`argocd app sync jenkins-ps3-k8s`
([ADR 0025](../adr/0025-singleton-controller-rollout-gating.md)). JCasC
hot-reloads clouds, no pod restart.

## Resize or retype a master

`just runbook master-resize`, or by hand:

1. Announce in #opensource-jenkins. The instance is replaced, builds in
   flight die.
2. Edit `on_demand_instance_type` in `terraform/master-<inst>.tf`, PR,
   merge.
3. `just tf-plan` (expect one replace, the identity volume re-attaches),
   then `just tf-apply`.
4. The web path converges through the endpoint reconciler about a
   minute after the new instance serves :8080.

## Bump Jenkins core

`just runbook core-bump`. Same announce-and-replace flow:
`jenkins_package_version` in `terraform/master-<inst>.tf` lands in
user-data, so the apply replaces the instance. Plugins are separate:
operator-driven on the EC2 masters, image-driven on ps3-k8s
(`images/jenkins/`).

## Engineer SSH keys

`just runbook ssh-key`. Edit `ssh_key_engineers` in
`terraform/master-<inst>.tf`. Keys are fetched from percona.com at
boot, so the change takes effect at the next instance replacement.
Urgent removal: also delete the key from
`/home/ec2-user/.ssh/authorized_keys` over SSM
([`master-shell-access.md`](master-shell-access.md)).

## Ports and SSH allow-list

`ssh_allowed_cidrs` (operator :22 sources) and `extra_http_ingress`
(port/CIDR pairs) in `terraform/master-<inst>.tf`. Plan and apply, in
place. Codify any hand-added emergency SG rule the same day, the next
apply strips manual edits.

## Graviton fleet capacity or types

`max_size` and `instance_types` on the `jenkins-arm-fleet` module call
in `terraform/master-<inst>.tf`. Plan and apply update the ASG in
place, running workers are untouched.

## After any change

- `just check-master-alloy`: every controller still ships telemetry.
- Probe build on the touched label: the only real proof for template
  work.
- `just tf-plan` back to "No changes": the convergence check after any
  out-of-band hotfix.

## Not here

EKS and addons ([`eks-upgrade.md`](eks-upgrade.md),
[`mng-label-taint-changes.md`](mng-label-taint-changes.md)), recovery
([`disaster-recovery.md`](disaster-recovery.md),
[`argocd-admin-recovery.md`](argocd-admin-recovery.md)), onboarding a
new host ([`add-jenkins-host.md`](add-jenkins-host.md)), and the
account reapers ([`cleanup-reapers.md`](cleanup-reapers.md)).
