<!-- Copyright (C) 2026 Percona LLC -->
# 0029 — Fleet agent-cloud templatization: shared catalog, per-master overlays, dual renderer

**Status:** Proposed (2026-06-02)
**Related:** [ADR 0024](0024-jenkins-fleet-ownership-boundary.md) (clouds, agent templates and init.groovy.d *content* are the JCasC+Git layer — this ADR structures that layer), [ADR 0025](0025-singleton-controller-rollout-gating.md) (a catalog change reloads live and is ungated unless it pulls a new plugin; cutover is windowed), [ADR 0027](0027-baked-jenkins-controller-image.md) (the EC2 + Hetzner fork plugins the clouds depend on are forced by the image), [ADR 0028](0028-jenkins-dynamic-config-data-lifecycle.md) (JCasC owns the Hetzner cloud, the init.groovy.d ConfigMap owns EC2 cloud generation; this ADR feeds both from one catalog)

## Context

Ten masters each hand-maintain their EC2 + Hetzner agent clouds: nine as 438–1094-line imperative Groovy (`cloud.groovy` + `htz.cloud.groovy`), and ps3 as a ~6650-line JCasC `ps3-clouds` configScript. The definitions are a table stored as ~10 parallel maps + a 38-arg positional constructor, duplicated within and across files: a one-line agent change (the `docker-buildx-plugin` add) had to be made in four places. ADR 0028 already split delivery (JCasC owns the Hetzner cloud; the init.groovy.d ConfigMap owns EC2 generation) but left the *content* hand-written and per-master. We need one source of truth for the agent-template catalog, with each master selecting from it, and a mechanical guarantee that a regenerated definition is equivalent to the one it replaces.

## Decision

1. **A shared catalog + per-master overlays** under `resources/jenkins/clouds-catalog/`: `catalog.yaml` (OS-family bundles, instance-type specs, device shapes, and each init body defined ONCE), and `masters/<host>.yaml` (the enabled-template subset + the per-master override axes: region, AZ list, subnets, IAM profile, ssh cred, tag/billing prefix, connection strategy, idle, maxUses, cap, and the cloud-type profile). AMIs are **committed literals** (refreshed upstream by the image pipeline), not resolved at render or runtime — see Alternatives.
2. **One generator (`scripts/render-clouds.py`, run via `uv`) with two serializers.** A JCasC configScript for the in-cluster master(s) (spliced into `instances/<host>/values.yaml`); `cloud.groovy` + `htz.cloud.groovy` for the EC2 masters (written into `resources/jenkins-masters/<host>/init.groovy.d/`). The generator resolves catalog + overlay into ONE canonical cloud model; JCasC and Groovy are serializers of that single model, never separate logic. The Groovy serializer emits the exact pinned positional `SlaveTemplate`/`EC2Cloud` ctors. No ApplicationSet or TF-module change — both consume the generated artifacts through existing seams (Helm valueFiles; `init_groovy_files` → S3 → user-data).
3. **Committed generated artifacts + a render-equivalence drift gate.** The generated files are committed and reviewable; `just ci` (`clouds-render-check`) regenerates and fails on any drift between the catalog and the committed configScript.
4. **"Same templates" = one catalog library + per-master enabled subsets**, not a universal set forced on every master (that would push EOL OSes, metal shapes and host-key-off SSH onto masters that never had them).
5. **Sequencing (governed by ADR 0025):** ps3 first because it is the **canary/testbed** master — adapting it exercises the whole pipeline against a *live* in-cluster JCasC reload at low stakes, so it is the safe place to prove both the generator and the gates; a bad reload there is a contained learning event. ps57 second as the first **production** EC2/Groovy master (full gating + live canary). Then the remaining masters (Hetzner near-trivial; AWS per-master). Initial scope is the five masters already TF-managed in this repo (ps3, ps57, ps80, pxb, pxc); the other five need their `terraform/master-<host>.tf` + `resources/jenkins-masters/<host>/` delivery path first. Converging the EC2 masters to JCasC is explicitly **out of scope** (ADR 0028 keeps EC2 on init.groovy.d for the per-AMI loop + `ami-defs.properties` fetch; a declarative `jenkins.clouds` prunes).

## Consequences

**(+)** One definition per template and per init body; add-an-OS is a row, the buildx-style change is one line; per-master files shrink to a compact table + constants; divergence becomes reviewable data, not forked code.
**(+)** ps3's 6650-line hand-written JCasC becomes generated and render-equivalent — reconstructible (ADR 0024).
**(+)** Both encodings come from one catalog, so the JCasC and Groovy masters can never silently diverge.
**(−)** A new generator + catalog schema to own; init bodies must be lifted byte-faithfully; the Groovy serializer is coupled to the fork's positional ctor (version-pinned, v5.24.percona.4).
**(−)** The render-equivalence + drift gate must be airtight or generated config silently diverges from live.
**(−)** A partial or mis-ordered `jenkins.clouds` prunes whatever it omits — mechanically real on any JCasC master. On ps3 (the testbed) this is contained and recoverable, which is exactly why ps3 goes first; the completeness gate + loaded-model check + live canary have their full force on the **production** masters.

## Alternatives considered

- **Kustomize.** Rejected. Patch/overlay only — no iteration/templating, so it cannot expand a catalog row into N template blocks (the inner DRY), cannot emit the Groovy target, and conflicts with the Helm/ArgoCD source type (ADR 0005). Wrong layer for a generation problem.
- **Helm Go-template only.** Works for JCasC but cannot emit `cloud.groovy` for the EC2 masters, and non-trivial logic in Go-template is brittle. Subsumed by the generator (one code path, two targets, unit-testable).
- **Jsonnet / CUE.** Powerful, but a new toolchain not in the repo's stack; overkill. A Python generator matches the existing `scripts/` + `check_versions.py` convention.
- **Generate-at-deploy.** Rejected in favour of committed artifacts: reviewable diffs, no deploy-time toolchain, and the drift gate is a simple regenerate-and-diff.
- **Dynamic AMI resolution** (TF `aws_ami` data sources, EC2-plugin AMI filters, SSM, or the legacy boot-time `ami-defs.properties` fetch). Rejected for now: breaks render-equivalence (live config hardcodes IDs; ps3 abandoned the boot-fetch for literals), forces AWS credentials into the credential-free CI drift gate or a runtime dependency, and is non-deterministic. Deferred: the agent-image build pipeline refreshes the committed AMIs on publish — dynamic upstream, static at render.
- **Converge EC2 masters to JCasC now.** Deferred (per ADR 0028); its own migration.

## Verification

A change cannot cut a master over until its tier-1 + tier-2 gates pass; tier-3 is the cutover itself.

**Tier 1 — `just ci`, credential-free, every commit (`clouds-render-check` in `validate`):** render-equivalence diff (regenerate → normalize → diff vs the committed configScript); the generated configScript survives the subchart `tpl` pass (the Sprig `iamInstanceProfile` resolves to the injected account id; `jenkins-chart-render-check.sh`); Groovy CONVERSION parse + the 38-arg positional ctor order matches the loaded fork signature (v5.24.percona.4; the source comment's `ec2-1.41` is stale); init-body byte-fidelity vs the legacy `initMap`/`userData`; completeness/pruning (the generated `jenkins.clouds` contains EXACTLY the live cloud set + counts — ps3: 2 amazonEC2 + 1 hetzner + 1 eC2Fleet, 27/27/15/1); idempotence; and no account-id leak. The configScript must also carry NO YAML anchors/aliases: JCasC (SnakeYAML) caps aliases at 50 and the reload throws `ConfiguratorException` above that, so the generator spells repeated nodes out fully (this was caught live on the ps3 testbed, the static render gates having accepted the aliased form).

**Tier 2 — pre-cutover, per master (the loaded model is the oracle, not the text):** reload the generated config on an ephemeral Jenkins with the same controller image + plugin set, fail on any `ConfiguratorException`, and diff the loaded `jenkins.clouds` field-vectors vs the current live export. For Groovy masters, the ctor-stub harness instantiates the generated `cloud.groovy` against the actual loaded fork (v5.24.percona.4) and compares the captured 38-arg field model.

**Tier 3 — cutover (windowed, ADR 0025).** ps3 (testbed): once tier-1 passes, deploy and observe the live reload directly. For the **production** masters: GO/NO-GO — quiet-down, one master at a time; a **live canary** provisions one agent per critical label class before declaring the master done; **delivery verification** (the EC2 user-data S3 fetch warns-and-continues, so confirm the S3 object etag + on-master file checksum match after `tofu apply`); **rollback proof** (a known-good prior artifact whose reload restores the prior cloud export).

`just ci` passes.
