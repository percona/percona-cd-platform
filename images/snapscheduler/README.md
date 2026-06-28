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

This image redistributes the upstream operator, so it carries the upstream terms,
not just ours:

- **Operator (`/manager` binary)** — AGPL-3.0-or-later (upstream backube; verified
  in the v3.5.0 source headers). Shipped as `LICENSE` (GNU AGPL v3 text).
- **`api/` CRD types** — additionally Apache-2.0. Shipped as `LICENSE.apache-2.0.txt`.
- **Our Dockerfile wrapper** — AGPL-3.0, consistent with this repo.
- **Linked Go modules + stdlib** — their own licenses; see `NOTICE`.

See `NOTICE` for attributions. The legal files are COPYed into the image (the
distroless stage has no package manager, so they live at `/licenses`).

**Follow-up:** generate the complete per-module license inventory with
`go-licenses` against the pinned tag and ship it under `/licenses` for a fully
machine-verified NOTICE.
