# syntax=docker/dockerfile:1
# Multi-arch (linux/amd64 + linux/arm64) build of the upstream backube
# snapscheduler operator. The operator binary is AGPL-3.0-or-later (the api/
# CustomResource types are Apache-2.0). Upstream ships the image amd64-only; the
# platform fleet is all-arm64, so we build the pinned release from source. To
# track a new upstream release, bump BOTH SNAPSCHEDULER_VERSION and
# SNAPSCHEDULER_COMMIT (the tag's commit SHA), then rebuild and bump the chart tag.
#
# The builder pins to $BUILDPLATFORM (the host arch) and Go cross-compiles to
# $TARGETARCH. This avoids emulating the amd64 Go toolchain under QEMU, which
# segfaults the compiler when the build host is arm64.
ARG SNAPSCHEDULER_VERSION=3.5.0
# Pinned commit of tag v3.5.0; verified at build time so a moved tag fails closed.
ARG SNAPSCHEDULER_COMMIT=46883ceb23a2e3dbf59fcc4e0ef59c96a6195ba6

FROM --platform=$BUILDPLATFORM golang:1.24@sha256:d2d2bc1c84f7e60d7d2438a3836ae7d0c847f4888464e7ec9ba3a1339a1ee804 AS builder
ARG TARGETOS
ARG TARGETARCH
ARG SNAPSCHEDULER_VERSION
ARG SNAPSCHEDULER_COMMIT
WORKDIR /workspace
RUN git clone --depth 1 --branch "v${SNAPSCHEDULER_VERSION}" \
      https://github.com/backube/snapscheduler.git . \
    && got="$(git rev-parse HEAD)" \
    && [ "${got}" = "${SNAPSCHEDULER_COMMIT}" ] \
       || { echo "tag v${SNAPSCHEDULER_VERSION} resolved to ${got}, expected ${SNAPSCHEDULER_COMMIT} (tag moved?)" >&2; exit 1; } \
    && go mod download \
    && CGO_ENABLED=0 GOOS="${TARGETOS:-linux}" GOARCH="${TARGETARCH}" \
       go build -a -o /manager \
       -ldflags "-X=main.snapschedulerVersion=v${SNAPSCHEDULER_VERSION}" cmd/main.go

FROM gcr.io/distroless/static:nonroot@sha256:963fa6c544fe5ce420f1f54fb88b6fb01479f054c8056d0f74cc2c6000df5240
ARG SNAPSCHEDULER_VERSION
ARG SNAPSCHEDULER_COMMIT
LABEL org.opencontainers.image.title="snapscheduler" \
      org.opencontainers.image.description="backube snapscheduler operator, in-house multi-arch build" \
      org.opencontainers.image.source="https://github.com/backube/snapscheduler" \
      org.opencontainers.image.version="${SNAPSCHEDULER_VERSION}" \
      org.opencontainers.image.revision="${SNAPSCHEDULER_COMMIT}" \
      org.opencontainers.image.licenses="AGPL-3.0-or-later"
WORKDIR /
COPY --from=builder /manager /manager
# Legal: license + attribution files. The distroless base has no package
# manager, so the image carries them at /licenses.
COPY LICENSE LICENSE.apache-2.0.txt NOTICE README.md /licenses/
USER 65532:65532
ENTRYPOINT ["/manager"]
