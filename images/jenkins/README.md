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
| `plugins.effective.txt` | Build-generated, committed archive of the fully-resolved plugin set (audit + determinism gate). Regenerate against the 2.541.3 base in CI. |
| `percona-plugins.lock.json` | Fork HPI manifest: `id` / `version` / `filename` / `url` / `sha256`. |
| `fetch-hpis.sh` | Downloads + SHA-verifies the fork HPIs into `percona-plugins/` before the build. |
| `percona-plugins/` | Staging dir for fetched fork plugins (staged as `<id>.jpi`). Holds only `.gitkeep` in git; `*.jpi`/`*.hpi` are gitignored (fetched at build time, never committed). |
| `groovy/` | `init.groovy.d` boot hooks (`persistent/`, `one-time/`), COPYed into the image. |

## Base image: pinned by DIGEST

```dockerfile
FROM jenkins/jenkins:2.541.3-lts-jdk17@sha256:<DIGEST>
```

2.541.3 matches the live EC2 fleet. The image is consumed on `linux/amd64` only
(EKS nodes are amd64; the m3 build host is arm64 -> buildx cross-builds). We pin
the **per-platform amd64 digest**, not the multi-arch index digest, so an
upstream re-push of the same tag cannot silently change our base.

Refresh the digest on every LTS bump:

```bash
crane digest --platform linux/amd64 jenkins/jenkins:2.541.3-lts-jdk17
# or, without crane:
docker buildx imagetools inspect jenkins/jenkins:2.541.3-lts-jdk17 \
  --format '{{ range .Manifest.Manifests }}{{ if eq .Platform.Architecture "amd64" }}{{ println .Digest }}{{ end }}{{ end }}'
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
