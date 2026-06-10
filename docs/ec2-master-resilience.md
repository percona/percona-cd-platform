# EC2 Jenkins master resilience

The eight Terraform-managed masters run as single ON-DEMAND instances
since the CFN→TF migrations (the wave completed 2026-06-10), which ended
the master-side spot reclamations; only pg's legacy CloudFormation master
still rides a SpotFleet. Without help, an abrupt instance loss or JVM
crash loses in-flight pipelines and reaps every Hetzner worker the master
had provisioned. This doc covers the four layered mechanisms the platform
puts in place — built for the spot era, retained as defense-in-depth —
and the specific cases where each one does not help. Worker-side spot
interruptions are a separate track (`retry(conditions: [agent()])` in the
pipelines, `disableTaskResubmit=true` on every arm fleet).

Validated end-to-end on the former EC2 `ps3` master via three FIS
spot-interruption experiments. `ps3` itself has since moved in-cluster
and its EC2 spot master was decommissioned on 2026-06-07 (see
[`runbooks/decommission-ps3-ec2-master.md`](runbooks/decommission-ps3-ec2-master.md));
the original `ps3` validation is kept as the reference run.

## The four mechanisms

| Mechanism | What it covers | Where it lives |
|---|---|---|
| **SpotFleet Capacity Rebalancing** | AWS launches a replacement on a rebalance recommendation, minutes to hours before a spot interruption notice. The replacement is booting while the old instance still serves traffic. | `terraform/modules/jenkins-master/main.tf` (`spot_maintenance_strategies.capacity_rebalance`) |
| **Graceful spot-interrupt drain** | A 30-second cron on the master detects `spot/instance-action` IMDS metadata and runs a script that: quietDown, polls `busyExecutors` up to 85 s, safeExit, copies the log, unmounts the data EBS. `flock` prevents concurrent runs. | `terraform/modules/jenkins-master/user-data.sh.tftpl` (`jenkins-graceful-stop.sh` install + cron) |
| **Pipeline durability** | `MAX_SURVIVABILITY` global default. Every pipeline step is persisted to disk so an abrupt JVM stop resumes at the same step on the next start. | `resources/jenkins-masters/<host>/init.groovy.d/durability.groovy` (S3-delivered; pg still from `Percona-Lab/jenkins-pipelines:IaC/pg.cd/`) |
| **Hetzner worker rehydrate** | After a hard JVM stop, the Percona-patched Hetzner plugin re-adopts surviving Hetzner VMs as Jenkins agents instead of letting `OrphanedNodesCleaner` reap them. DC circuit-breaker state is also persisted so a restart does not stampede a still-sick datacenter. | `Percona-Lab/jenkins-hetzner-cloud-plugin` (rehydrate since v103.percona.22), `init.groovy.d` |

The four layers compose: rebalancing reduces the chance of needing the
2-minute drain at all; the drain handles the case AWS gives no advance
warning; durability covers everything that did not finish during the
drain; rehydrate brings the workers back when the JVM died too abruptly
to disconnect them cleanly.

## Operational check

`scripts/check-master-spot-readiness.sh <inst>` walks every moving piece
(SpotFleet config, cron daemon + watcher, graceful-stop.sh + flock,
JVM args, Secrets Manager fetch, api-admin auth probe). Exit 0 means
the master will respond correctly; non-zero shows which stage failed.

Run it before declaring a master "spot-ready" and as a smoke-test after
any userdata, plugin, or `init.groovy.d` change.

## Rehydrate: when workers are kept across a reboot

Rehydrate works for *abrupt* JVM stops where workers are not given a
chance to disconnect:

- **Kill -9 of the JVM** (OOM, hard fault, manual `kill -9`)
- **Hard EC2 reboot** without going through `systemctl stop jenkins`
- **AWS spot interruption that outruns the graceful drain** (e.g. the
  drain is mid-`safeExit` and `umount` returns "target busy" before
  cleanly stopping the JVM; AWS terminates ~15 s later regardless)

In each case the JVM dies before `Computer.disconnect()` fires, so the
Hetzner VMs keep running. When the replacement boots, the rehydrate
`@Initializer` queries the Hetzner API by `cloud-name` label and re-adds
each surviving VM as a Jenkins agent. `ControllerListener.onOnline`
defers `OrphanedNodesCleaner` by 5 minutes so the rehydrate pass has
time to claim VMs first.

Validated on the `ps3` FIS run: 1/1 VM re-adopted on the post-FIS
replacement instance, agent name preserved, no manual intervention.

## Rehydrate: when workers are still lost

These are the cases where workers will be reaped or unusable despite the
rehydrate path being in place. Most are intentional teardown; a few are
configuration drift that the readiness check catches.

### 1. Graceful shutdown (safeExit)

Any planned restart that goes through Jenkins's normal shutdown path
calls `Computer.disconnect()` on each agent, which the Hetzner plugin
turns into `HetznerServerAgent.terminate()`, which deletes the VM via
the Hetzner API.

This includes:

- `systemctl restart jenkins`
- `jenkins admin restart` (REST `/restart`)
- The graceful drain script's `safeExit` step when busyExecutors hits 0
- Any operator-initiated quiet-then-restart

There are no surviving VMs for rehydrate to claim. **This is intentional
and correct.** Rehydrate is only meant for abrupt stops.

### 2. Rehydrate flag not set on the JVM

The `@Initializer` and the cleanup defer in `ControllerListener.onOnline`
both gate on `Boolean.getBoolean("hetzner.rehydrate.enabled")`. Without
it:

- `ControllerListener.onOnline` fires `OrphanedNodesCleaner.doCleanup()`
  immediately on master start (upstream behaviour, intended to reap
  workers left from a different master).
- Every Hetzner VM labeled with this cloud is reaped within seconds.

The userdata bakes the flag in via the `JAVA_OPTS` line in the
systemd override, gated by `master_profile == "eks_observability"` (only
the former EC2 `ps3` ever used that profile; its spot master has since been
decommissioned). On any master without the flag the rehydrate path is a
no-op.

`check-master-spot-readiness.sh` shows this as `FAIL rehydrate flag in
JVM args`.

### 3. Plugin version drift

The rehydrate code and the `LABEL_TEMPLATE_NAME` label were added in
`v103.percona.22`. The nine production masters still on
`v103.percona.16` / `.20` / `.21` do not have either, so even if the
flag is set, the `@Initializer` class does not exist and the immediate
cleanup is not deferred.

Fix is a fleet plugin upgrade; tracked separately.

### 4. Cloud config rename or drift

The rehydrate selector filters Hetzner VMs by `jenkins.io/cloud-name=<name>`.
The name is the Jenkins `HetznerCloud.name` from `htz.cloud.groovy`. If
that name changes (`ps3-htz` → `ps3-hetzner`, etc.), existing VMs still
carry the old label and are unmatched.

Result: the rehydrate pass logs an INFO line (`cloud=<name> has 0 VMs`),
the cleaner runs at the 5-minute mark, and any old-labelled VMs are
reaped or simply ignored.

### 5. Template heuristic miss

Each VM is matched to a Jenkins template via two strategies:

1. Exact match on the `LABEL_TEMPLATE_NAME` label (`jenkins.io/template-name`).
   This is the v103.percona.22 happy path.
2. Heuristic fallback: serverType + location + the configured name prefix.

VMs provisioned by a pre-`.22` plugin do not carry the template-name
label, so they fall to the heuristic. If two templates qualify, the
heuristic logs `ambiguous` and skips the VM. If none qualify, it logs
`no_match` and skips. Skipped VMs sit there until the deferred cleaner
fires at +5 min.

Metric counters expose this: `hetzner_rehydrate_failures_total{reason="ambiguous"}`
/ `{reason="no_match"}`.

### 6. Fresh-EBS rebuild

The data EBS volume survives instance replacement (this is the common
case). But on actual volume loss or snapshot restore, the new master
gets a fresh `$JENKINS_HOME`:

- `credentials.xml` is gone → rehydrate creates the agent record, but
  the SSH launcher has no key, so each agent comes online and then
  immediately goes offline.
- The `api-admin` user is gone → the graceful drain on the next spot
  interrupt can fetch the Secrets Manager token but Jenkins rejects
  it; the drain falls back to `systemctl stop jenkins`.
- DC circuit-breaker state is gone → the first restart can stampede a
  still-sick datacenter (until the breakers re-trip on live failures).

Mitigation: idempotent `init.groovy.d` for the `api-admin` user
(deferred); baking persisted state into snapshots is the proper fix.

### 7. Hetzner API failure during the rehydrate pass

`HetznerCloudResourceManager.fetchAllServers` errors are caught at the
`@Initializer` level. The pass logs the error and returns; it does
**not** retry. If the API was flaking during exactly that window:

- The rehydrate pass adopts zero VMs.
- The cleanup defer still runs.
- At +5 min the cleaner runs and finds the same VMs still on Hetzner
  (the cleaner is per-cloud, not per-VM-state). Behaviour from there is
  the existing upstream cleaner logic.

Worth filing a backoff/retry follow-up if this happens in practice.

### 8. Idle reap shortly after rehydrate

Each Hetzner template carries an `IdlePeriodPolicy` shutdown policy
(default 30 min idle). The policy is enforced by the master-side
plugin `PeriodicWork`, so during master downtime nothing reaps idle
workers. But once the new master is up and has rehydrated the VMs,
the timer starts counting normally.

If a re-adopted worker stays idle for the configured period right
after rehydrate (no jobs land on it), the plugin will reap it. This
is not a rehydrate bug; it is the same idle policy that always
applies.

## See also

- [`check-master-spot-readiness.sh`](../scripts/check-master-spot-readiness.sh) — the operational audit
- [`adr/0013-push-from-masters-with-nginx-bearer.md`](adr/0013-push-from-masters-with-nginx-bearer.md) — alloy push pipeline (parallel resilience story for observability)
- `Percona/percona-jenkins` skill (private) and `Percona/percona-hetzner` skill — fleet-wide ops notes for the masters
