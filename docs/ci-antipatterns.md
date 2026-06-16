# CI antipatterns

A living tracker of CI and Jenkins antipatterns seen on this platform: what the
pattern is, why it hurts, how to spot it, and what to do instead. Add an entry
whenever an incident or a review surfaces a recurring "don't do this".

Per entry: **What**, **Why it's a problem**, **How to spot it**, **Use instead**,
**Seen in / evidence**, **Status**.

## 1. Build or release steps on the Jenkins controller

**What.** A pipeline or stage pinned to the built-in node with
`agent { label 'master' }`, so its `sh` steps execute on the Jenkins controller
host itself instead of on an agent.

**Why it's a problem.**
- It couples the job to whatever happens to be installed on the controller, so a
  controller rebuild or base-image change silently breaks the job.
- Build work contends with the controller JVM and its scheduling.
- Blast radius: arbitrary job shell runs on the control plane, next to
  `JENKINS_HOME` and on-disk credentials.
- The Terraform masters are now on-demand cattle, rebuilt on the al2023-minimal
  AMI, so anything a job relies on being present on-box can vanish on replacement.

**How to spot it.** A pipeline-level or stage-level `agent { label 'master' }` in
a Jenkinsfile, or `sh` steps that assume host tools and paths the controller
happens to have.

**Use instead.** Run build and release stages on a worker agent. The controller
should schedule work, not execute it. If a stage needs a tool, install it on the
worker (or in the stage), rather than depending on the controller's image.

**Seen in / evidence.** `pmm/v3/pmm3-release.groovy` runs on
`agent { label 'master' }`, and its Publish Docker and Publish OVF stages call
`dig` (from `bind-utils`) to resolve `downloads-rsync-endpoint.int.percona.com`
before an `scp` upload. The CFN to TF migration rebuilt the pmm master on the
al2023-minimal AMI, which omits `bind-utils`, so `dig: command not found` broke
the upload (reported 2026-06-16). Those stages ran on the controller only by
convention plus obscurity (a non-standard SSH port and an internal-only DNS
name), not necessity: a `min-ol-9-x64` worker in the same VPC, on a different
subnet and IP, authenticates to the downloads host with the same `jenkins-deploy`
key and reaches the upload path identically, with no source restriction. The
minimal-AMI tool gap itself is fixed durably in the `jenkins-master` module's
boot-time package install (best-effort, so a missing tool warns instead of
aborting boot).

**Status.** Open. Move the `pmm3-release` Publish stages to a worker label, and
audit other pipelines for `agent { label 'master' }`.
