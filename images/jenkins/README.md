# Jenkins controller image (`percona-cd/jenkins-percona`)

The baked Jenkins controller image for the Percona CI masters: upstream Jenkins
LTS + the community plugin set (`plugins.txt`) + the two patched Percona plugin
forks (Hetzner cloud, EC2 cloud). Built and pushed to ECR by
`.github/workflows/build-jenkins-image.yml`.

## Layout

| Path | Purpose |
|------|---------|
| `Dockerfile` | Image definition. Base pinned BY DIGEST (see below). |
| `plugins.txt` | Community plugin manifest (pinned versions). |
| `plugins.effective.txt` | Generated **inside the image** at `/usr/share/jenkins/ref/plugins.effective.txt` (the fully-resolved plugin set, for audit). Not committed to the repo. |
| `percona-plugins.lock.json` | Fork HPI manifest: `id` / `version` / `filename` / `url` / `sha256`. |
| `fetch-hpis.sh` | Downloads + SHA-verifies the fork HPIs into `percona-plugins/` before the build. |
| `percona-plugins/` | Staging dir for fetched fork plugins (staged as `<id>.jpi`). Holds only `.gitkeep` in git; `*.jpi`/`*.hpi` are gitignored (fetched at build time, never committed). |
| `groovy/` | `init.groovy.d` boot hooks (`persistent/`, `one-time/`), COPYed into the image. |

## Base image: pinned by DIGEST

```dockerfile
FROM jenkins/jenkins:2.541.3-lts-jdk17@sha256:<DIGEST>
```

2.541.3 matches the live EC2 fleet. The image is built **multi-arch**
(`linux/amd64` + `linux/arm64`) so it can run on Graviton EKS nodes; CI builds
both arches (arm64 via QEMU on the amd64 runner) and pushes one manifest list. We
pin the **multi-arch index digest**, so an upstream re-push of the same tag cannot
silently change our base.

Refresh the digest on every LTS bump (use the multi-arch index digest, not a
per-platform child):

```bash
docker buildx imagetools inspect jenkins/jenkins:2.541.3-lts-jdk17 \
  --format '{{ .Manifest.Digest }}'
```

Substitute the printed `sha256:...` into the `FROM` line and update the
`Resolved <DATE>:` comment so the next bump is auditable.

## Fork plugins: SHA-verified fetch, never committed

`fetch-hpis.sh` reads `percona-plugins.lock.json`, downloads each fork release
asset to a temp file, verifies its `sha256` against the lock, and only then moves
it into `percona-plugins/` as `<id>.jpi` (the plugin short-name with the `.jpi`
extension, NOT the versioned asset name: Jenkins derives the plugin id from the
ref-dir filename). A tampered or mismatched asset never lands; the script
hard-fails on any non-hex `sha256`. Both forks are public `Percona-Lab/*` GitHub
Releases fetched with `curl`, so the PR validation job needs no GitHub token. The
`sha256` pins the published-asset bytes (the release workflow rebuilds the HPI, so
the lock records the release sha, not a local build).

## Keeping the forks current: recorded-pin auto-bump

The lock is a **recorded pin**, not a float: the build always fetches an exact,
sha-verified version. `scripts/refresh-fork-locks.sh` is what moves the pin
forward. For each entry it derives the GitHub repo from the entry's own `url`,
lists that repo's releases (public, so no token needed to list), keeps only
non-draft, non-prerelease tags carrying a `.percona.` version, picks the highest
with `sort -V` (numeric-aware: `.9 < .10 < .26`), and only if that is strictly
newer than the locked version it downloads the single `<id>-<ver>.hpi` asset,
runs `unzip -t`, checks the embedded MANIFEST `Short-Name` + `Plugin-Version`,
computes the sha256 (cross-checked against the published `.hpi.sha256` sidecar),
and rewrites that entry. It NEVER edits the Dockerfile, `fetch-hpis.sh`, or the
deployed image tag.

```bash
just refresh-fork-locks    # rewrite the lock to the latest fork releases (if newer)
just check-fork-locks      # report only; exit 3 if a newer release is available
```

`.github/workflows/refresh-fork-locks.yml` runs it weekly (and on demand). If the
lock moves, the job re-runs `fetch-hpis.sh` + the Docker build + `smoke-boot.sh`
against the new pin BEFORE opening a PR, so a bad bump fails in the refresh run.
The PR is **never auto-merged**: CODEOWNERS approves, and because the PR touches
`images/jenkins/**` the `build-jenkins-image` validation runs on it too. Set the
optional repo secret `FORK_LOCK_BUMP_TOKEN` (a PAT/App token) so the opened PR
re-triggers that validation; the commit itself is signed via the GitHub API
(`createCommitOnBranch`), so CI needs no GPG key. The deployed image tag in
`resources/jenkins/master/values-base.yaml` stays a separate, manual bump
(ADR 0025): merging a lock PR does not change the running controller.

## Plugin reconciliation: HYBRID (forks forced, community soft)

The chart sets `installPlugins: false` and `overwritePluginsFromImage: false`.
In the upstream `jenkins` chart, `overwritePluginsFromImage` only does anything
when `installPlugins: true`, so with `installPlugins: false` it is **inert**, and
`/usr/share/jenkins/ref/plugins/` seeds `$JENKINS_HOME/plugins` per the ref-dir
seeder's own rules: a plain `<id>.jpi` is seeded ONLY when the home lacks that
plugin or carries an OLDER build (version-compare), while a `<id>.jpi.override`
is force-installed UNCONDITIONALLY (its content is COPYed over
`$JENKINS_HOME/plugins/<id>.jpi` on every boot). On a PVC restored from a real
EC2 master's `$JENKINS_HOME` the plugins dir is already populated, so for a plain
`.jpi` the on-disk copy wins unless the baked one is strictly newer.

We exploit those two behaviors deliberately:

- **Patched forks (`ec2`, `hetzner-cloud`) are FORCED.** The Dockerfile renames
  ONLY the two fork plugins to `<id>.jpi.override`, so the patched build always
  wins over whatever a PVC-restored home carries. The `.override` file must
  therefore BE the plugin: an empty `.override` marker would be copied over the
  plugin as 0 bytes and it would fail to load (`ZipException: archive is not a
  ZIP archive`). Fork files are COPYed `--chown=jenkins:jenkins` so the seeder
  (which runs as the jenkins user) can read them.
- **Community plugins stay SOFT.** They are left as plain `<id>.jpi`, so the
  seeder version-compares and seeds only when the home lacks the plugin or has an
  older build. A community plugin a human installed or upgraded via the Jenkins
  UI on a live master is NOT clobbered on the next pod restart unless the image
  pins a newer version. The image is the **floor** for community plugins, the
  **source of truth** for the forks.

We do NOT set `overwritePluginsFromImage: true` (inert here, and it would force
the WHOLE baked set, defeating the soft-community half of this policy), and we do
NOT strip plugins from the seed snapshot (a one-time, manual, error-prone step
that must be redone on every new snapshot, is invisible in code review, and loses
the byte-identical-to-EC2 restore property).

> **Operator note:** the two forks win on EVERY pod boot, so to change a fork
> version rebuild the image (bump `percona-plugins.lock.json`); a hand-edit on
> the PVC is reset on the next restart. Community plugins are the opposite: a UI
> upgrade persists, so pin a community plugin in `plugins.txt` and rebuild when
> you want it enforced rather than merely floored.

## Image reference: TAG, not digest (today)

The chart pins the image by an **immutable SHA-TAG** `<lts>-<gitsha>` (e.g.
`2.541.3-a1b2c3d`), not by a true `@sha256:` digest. The upstream `jenkins`
subchart (5.9.18) has no `digest` key, so a real digest pin waits on the pilot
chart rebuild. The ECR repo is IMMUTABLE, so the SHA-tag cannot be overwritten,
which gets most of the immutability benefit. Be honest in bump PRs: this is a
**TAG bump, not a digest bump**. The build records the actual pushed `@sha256:`
digest in the PR body for traceability.

## CI

`.github/workflows/build-jenkins-image.yml`:

- **`validate`** (pull requests): build + smoke-boot, NO AWS token, NO push.
  Runs end-to-end on the Hetzner-only image today.
- **`publish`** (push to `main`): the only job with `id-token: write`. Builds and
  smoke-boots BEFORE push, assumes the scoped OIDC role, pushes to ECR with SBOM
  + provenance, captures the pushed digest, then re-smokes the pushed `@sha256:`
  digest. Third-party actions are pinned by commit SHA.

`smoke-boot.sh` boots the built image and asserts each baked fork plugin loads at
its LOCKED version. By default it runs the empty-home boot; set
`SMOKE_RESTORED_HOME_TAR` to a real `$JENKINS_HOME` tar to also exercise the
`.override`-wins-over-a-populated-PVC path (the only test that proves the
override). Wiring a restored-home fixture into CI is a follow-up.

`.github/workflows/refresh-fork-locks.yml` (recorded-pin auto-bump, above) is the
third workflow: scheduled, opens a build+smoke-validated lock-bump PR, no push.
