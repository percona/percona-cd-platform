# snapscheduler (in-house multi-arch build)

Multi-arch (`linux/amd64` + `linux/arm64`) build of the upstream
[backube/snapscheduler](https://github.com/backube/snapscheduler) Kubernetes
operator, which takes scheduled VolumeSnapshots of PVCs. Upstream publishes the
image **amd64-only**; the percona-ci-platform fleet is all-arm64 Graviton, so we
build the pinned release from source.

## Build

| Knob | Purpose |
|------|---------|
| `SNAPSCHEDULER_VERSION` | Upstream release tag (e.g. `3.5.0`). |
| `SNAPSCHEDULER_COMMIT` | The tag's commit SHA, **verified at build time** so a moved tag fails closed. |
| `golang:1.24@sha256:...` | Builder, pinned by digest. Matches upstream `go.mod` (`go 1.24.0` / `toolchain go1.24.3`). Builder-only; the final image is `distroless/static`. |

The builder pins to `$BUILDPLATFORM` and Go cross-compiles to `$TARGETARCH`,
avoiding the QEMU-emulated amd64 toolchain segfault on the arm64 build host. CI
builds both arches and pushes one manifest list.

To track a new upstream release: bump `SNAPSCHEDULER_VERSION` **and**
`SNAPSCHEDULER_COMMIT`, refresh the `golang` base digest if upstream's `go.mod`
moved, rebuild, then bump the chart image tag.

## Licensing

This image redistributes the upstream backube operator, so it carries the upstream
terms: the operator (`/manager`) is AGPL-3.0-or-later and the `api/` types are
additionally Apache-2.0; our Dockerfile wrapper is AGPL-3.0. See `LICENSE`,
`LICENSE.apache-2.0.txt`, and `NOTICE` (COPYed into the image at `/licenses`).
