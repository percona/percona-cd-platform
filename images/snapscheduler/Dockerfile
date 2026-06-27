# syntax=docker/dockerfile:1
# Multi-arch (linux/amd64 + linux/arm64) build of the upstream backube
# snapscheduler operator (Apache-2.0). Upstream ships the image amd64-only; the
# platform fleet is all-arm64, so we build the pinned release from source. Bump
# SNAPSCHEDULER_VERSION to track an upstream release, then rebuild and bump the
# chart tag.
#
# The builder pins to $BUILDPLATFORM (the host arch) and Go cross-compiles to
# $TARGETARCH. This avoids emulating the amd64 Go toolchain under QEMU, which
# segfaults the compiler when the build host is arm64.
ARG SNAPSCHEDULER_VERSION=3.5.0

FROM --platform=$BUILDPLATFORM golang:1.24@sha256:39d9e7d9c5d9c9e4baf0d8fff579f06d5032c0f4425cdec9e86732e8e4e374dc AS builder
ARG TARGETOS
ARG TARGETARCH
ARG SNAPSCHEDULER_VERSION
WORKDIR /workspace
RUN git clone --depth 1 --branch "v${SNAPSCHEDULER_VERSION}" \
      https://github.com/backube/snapscheduler.git . \
    && go mod download \
    && CGO_ENABLED=0 GOOS="${TARGETOS:-linux}" GOARCH="${TARGETARCH}" \
       go build -a -o /manager \
       -ldflags "-X=main.snapschedulerVersion=v${SNAPSCHEDULER_VERSION}" cmd/main.go

FROM gcr.io/distroless/static:nonroot@sha256:963fa6c544fe5ce420f1f54fb88b6fb01479f054c8056d0f74cc2c6000df5240
WORKDIR /
COPY --from=builder /manager /manager
USER 65532:65532
ENTRYPOINT ["/manager"]
