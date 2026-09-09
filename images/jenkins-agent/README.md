# Jenkins agent-image factory

Packer factory for the worker **agent AMIs** (strategy layer 1): one baked
image per (os, arch, capability profile) carrying the fleet-common payload
every worker label family installs at boot today. Baking removes the measured
multi-minute per-launch setup and the boot-time network dependencies.

First delivered images: `jenkins-agent-al2023-{x86_64,arm64}`. The arm64
profile bakes qemu user-mode emulation (persistent binfmt via systemd) so
x86_64-only containers run on Graviton workers.

## Layout (extension recipe)

| Path | Role |
|------|------|
| `agent.pkr.hcl` | The single parameterized template. New (os, arch) combos extend variables, never copy the file |
| `provisioners/00-common.sh` | Fleet-common payload (Java 21, git, docker, awscli, archive tools and license files) |
| `provisioners/10-qemu-binfmt.sh` | arm64 capability profile: digest-pinned qemu + persistent binfmt. Self-guards on arch |
| `smoke/` | Fresh-boot runtime and emulation checks. This is not a Jenkins agent connection test |
| `justfile` | Local recipes mirroring the workflow: `fmt-check`, `validate`, `bake`, `smoke`, `promote`, `list` |

To add an image family: add a provisioner script (numbered, self-guarded),
extend the smoke assertions, and if a new OS is involved add its base-AMI SSM
parameter mapping in `agent.pkr.hcl`. The workflow matrix picks it up via the
`arches`/vars inputs.

## Lifecycle

Bake (candidate, `role=jenkins-agent-candidate`) -> fresh-boot smoke ->
promote (`role=jenkins-agent` + `smoke=passed` tag flip). Failed smokes
deregister the candidate; consumers resolving by role never see an unsmoked
image. Superseded images age out via native EC2 image deprecation; pruning
follows the fail-safe rails of the ppg factory prune (keep >= 2, protected
roles, loud no-op).

## Consumption

Worker fleets consume committed per-region literal AMI ids (ADR 0029), bumped
by PR after promotion. Adopting a baked image requires a real Jenkins worker
canary, starting with pxb, before changing any fleet's `ami_id`.
Slim the boot user data only after that canary proves the image, retaining
the instance-shape-specific steps
(ephemeral /mnt mount, docker data-root) that stay out of the bake.

## Verification boundary

The smoke checks the native CPU architecture, Java 21, Docker access as the
agent user, packaged license files and remoting class loading from
`jenkins_url` (default: the pxb controller). On arm64 it also checks persistent
binfmt registration and an x86_64 container. Remoting's `-version` verifies
that the JVM can load it, not that an agent connects or runs a Jenkins job.

The JVM requirement follows the
[Jenkins Java support policy](https://www.jenkins.io/doc/book/platform-information/support-policy-java/).
Local command-boundary regressions run with:

```sh
uv run --no-project python -m unittest discover -s images/jenkins-agent/tests -v
```

These tests also run in the workflow's credential-free validation job.
They do not replace a real candidate bake, fresh boot or worker canary.
`LICENSE`, `NOTICE`, this README and `LICENSE.qemu` are installed in
`/licenses/`. Preserve upstream corresponding-source obligations when
redistributing the extracted QEMU binary.

## Roadmap

- Per-region `copy-image` + per-region smoke before multi-region adoption
  (the factory bakes in one region; consumer fleets span five).
- Weekly refresh cron once the pxb canary proves the images.
- Hetzner snapshots via the `hcloud` builder in this same directory.
- Rocky/native-OS `min-*` profiles ride existing demand.
