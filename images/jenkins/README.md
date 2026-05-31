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
| `percona-plugins/` | Staging dir for fetched HPIs. Holds only `.gitkeep` in git; the `*.hpi` are gitignored (fetched at build time, never committed). |
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

`fetch-hpis.sh` reads `percona-plugins.lock.json`, downloads each fork HPI to a
temp file, verifies its `sha256` against the lock, and only then moves it into
`percona-plugins/`. A tampered or mismatched asset never lands. The script
hard-fails on any non-hex `sha256` (e.g. the EC2 TODO placeholder), so the build
cannot proceed with an unpinned fork. Public release assets are fetched with
`curl`, so the PR validation job needs no GitHub token.

The lock `url` host is `nogueiraanderson/*` for now; repoint both `url` fields to
`Percona-Lab/*` once the forks snapshot-copy there. `sha256` is content-addressed
and does NOT change with the host.

## Baked-image-is-source-of-truth: the `.override` decision

The chart sets `installPlugins: false` and `overwritePluginsFromImage: false`.
In the upstream `jenkins` chart, `overwritePluginsFromImage` only does anything
when `installPlugins: true`, so with `installPlugins: false` it is **inert**, and
`/usr/share/jenkins/ref/plugins/` seeds `$JENKINS_HOME/plugins` ONLY when that
target is empty. On a PVC restored from a real EC2 master's `$JENKINS_HOME`, the
plugins dir is already populated, so the OLD on-disk plugins win and a newer
baked HPI is silently ignored.

To make the baked image authoritative, the Dockerfile writes a sibling
`<id>.jpi.override` marker for every plugin in `/usr/share/jenkins/ref/plugins/`.
Jenkins core's ref-dir copy logic treats `.override` as "copy this ref plugin
over the existing `$JENKINS_HOME` copy even if one is already present", so the
baked set wins deterministically over a PVC-restored home.

We do NOT set `overwritePluginsFromImage: true` (it is inert here and would
mislead future readers), and we do NOT strip plugins from the seed snapshot (a
one-time, manual, error-prone step that must be redone on every new snapshot, is
invisible in code review, and loses the byte-identical-to-EC2 restore property).

> **LOUD WARNING for operators:** `.override` makes the image win on EVERY pod
> boot. A plugin that a human pinned or downgraded directly on a live master's
> PVC will be reset to the baked version on the next pod restart. That is the
> intended GitOps behavior (the image is the source of truth), but to change a
> plugin version you MUST rebuild the image, not hand-edit the PVC.

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

`smoke-boot.sh` (a separate IMG-Step-8 deliverable) must boot against BOTH an
empty home AND a restored/sanitized real `$JENKINS_HOME`, and assert both fork
plugins load at the LOCKED versions: that is the only test that proves
`.override` actually wins over a populated PVC.
