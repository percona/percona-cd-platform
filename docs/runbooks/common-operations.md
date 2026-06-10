# Common operations

The day-to-day changes, ranked by how often they actually happened in the
last year of fleet history, each with its procedure in this repo. The
golden rule throughout: config changes never replace instances, and
instance changes are announced first.

## Change a worker template on an EC2 master

The most common operation by far: add a distro label, bump a worker AMI,
swap instance types, or extend the agent init script.

1. Edit the master's config under
   `resources/jenkins-masters/<inst>/init.groovy.d/`:
   `cloud.groovy` for classic EC2 templates (labels, AMIs, types, init
   script), `htz.cloud.groovy` for Hetzner templates,
   `ec2FleetCloud.groovy` for the Graviton fleet wiring, `matrix.groovy`
   for matrix-job label axes. On `cloud` the file is `cloud.groovy.tftpl`,
   keep the `${...}` template variables intact.
2. PR, merge, then `just tf-plan && just tf-apply`. The plan shows only
   S3 object updates for that master's init-config bucket. No instance
   resources change.
3. Propagation: the SSM association re-syncs the bucket to disk within
   30 minutes, and the script takes effect at the next JVM start. For
   immediate effect on the live controller, evaluate it in place:
   `jenkins admin -i <inst> groovy -f <file>` (init.groovy.d is boot-only
   by itself).
4. Verify: run a probe build on the changed label, or check the template
   inventory with the jenkins CLI.

## The same change on the in-cluster master

ps3-k8s does not use init.groovy.d for clouds. Edit the shared catalog
under `resources/jenkins/clouds-catalog/` (per-master overlay
`masters/ps3.yaml`), run `just clouds-render-check`, merge, then sync
manually inside a window: `argocd app sync jenkins-ps3-k8s`
([ADR 0025](../adr/0025-singleton-controller-rollout-gating.md)). JCasC
hot-reloads clouds without a pod restart.

## Resize or retype a master

Edit `on_demand_instance_type` in `terraform/master-<inst>.tf`, plan,
announce in #opensource-jenkins, apply. The instance is replaced: the
identity volume detaches and reattaches to the new instance, and the web
path converges through the endpoint reconciler within about a minute of
the new instance serving :8080. Builds in flight do not survive a
replacement, schedule it like a restart.

## Bump Jenkins core on a master

`jenkins_package_version` in `terraform/master-<inst>.tf`. The version
lands in user-data, so the apply replaces the instance, treat it exactly
like a resize (window plus announcement). Plugins are separate:
fleet-wide plugin rollout is operator-driven on the EC2 masters and
image-driven on ps3-k8s (the locked manifest in `images/jenkins/`).

## Add or remove an engineer's SSH key

`ssh_key_engineers` in `terraform/master-<inst>.tf`. Keys are fetched at
boot from percona.com, so the change takes effect at the next instance
replacement, not immediately. For urgent removal, also delete the key
from `/home/ec2-user/.ssh/authorized_keys` over SSM on the running
master ([`master-shell-access.md`](master-shell-access.md)).

## Open a port or change the SSH allow-list

`ssh_allowed_cidrs` (operator :22 sources) and `extra_http_ingress`
(additional port/CIDR pairs) in `terraform/master-<inst>.tf`. Codify any
hand-added emergency rule the same day, the next apply strips manual SG
edits.

## Change Graviton fleet capacity or types

The `jenkins-arm-fleet` module call in the same `master-<inst>.tf`:
`max_size`, `instance_types`. Plan and apply update the ASG in place,
running workers are untouched.

## After any change

- `just check-master-alloy` confirms every controller still ships
  telemetry.
- A probe build on the touched label is the only real proof for template
  work.
- `tofu plan` back to "No changes" is the convergence check after any
  out-of-band hotfix.

## What deliberately is not here

EKS and addon operations ([`eks-upgrade.md`](eks-upgrade.md),
[`mng-label-taint-changes.md`](mng-label-taint-changes.md)), recovery
procedures ([`disaster-recovery.md`](disaster-recovery.md),
[`argocd-admin-recovery.md`](argocd-admin-recovery.md)), onboarding a new
host ([`add-jenkins-host.md`](add-jenkins-host.md)), and the account
reapers ([`cleanup-reapers.md`](cleanup-reapers.md)).
