# jenkins-endpoint-reconciler

A small Percona-authored Python job that keeps the Kubernetes `EndpointSlice` for each
EC2-hosted Jenkins master pointed at the master's current private IP, so the in-cluster
`jenkins-ingress` proxy can reach masters that live outside the cluster and move when a
SpotFleet replaces them (ADR 0019, PS-10945).

For each host in `HOSTS_JSON` the job:

1. Calls `ec2:DescribeInstances`, tag-filtered by `iit-billing-tag=<tag>` and
   `instance-state-name=running`, in the host's region.
2. HTTP-probes each candidate on the Jenkins port and requires the `X-Jenkins` response
   header, so only a real master's IP is ever written (newest serving wins a multi-match;
   a no-server result leaves the existing slice untouched).
3. Writes the `EndpointSlice jenkins-<name>` in `TARGET_NAMESPACE`. The matching ClusterIP
   Service selects nothing, so the EndpointSlice is the sole way pods are advertised.

It is designed to run on a 1-minute CronJob and exits non-zero only on real boto3 /
kubernetes API errors. An empty discovery (no running master) is a valid steady state during
SpotFleet replacement: the slice is written empty and the proxy serves its 503 page until the
next run picks up the new IP.

## What it is built from

- **Base image:** `ghcr.io/astral-sh/uv:python3.13-bookworm-slim`, pinned by multi-arch index
  digest so an upstream re-push of the same tag cannot change the base. This base bundles the
  `uv` installer (astral-sh/uv, Apache-2.0 OR MIT), CPython 3.13 (PSF License Agreement), and a
  Debian 12 "bookworm" slim userland (Debian base packages under their respective licenses).
- **Python dependencies**, installed at build time with `uv pip install --system`:
  - `boto3` 1.36.x (Apache-2.0) and its stack: `botocore`, `s3transfer` (Apache-2.0),
    `jmespath` (MIT), `python-dateutil` (Apache-2.0 AND BSD-3-Clause), `urllib3` (MIT),
    `six` (MIT).
  - `kubernetes` 31.x (Apache-2.0) and its stack: `requests`, `google-auth`,
    `websocket-client`, `rsa` (Apache-2.0), `urllib3`, `pyyaml`, `cachetools`,
    `durationpy`, `charset-normalizer` (MIT), `requests-oauthlib` (ISC), `oauthlib`,
    `idna`, `pyasn1`, `pyasn1-modules` (BSD), `certifi` (MPL-2.0), `six`, `python-dateutil`.
- **Application code:** `reconcile.py`, original to Percona, under AGPL-3.0 (see LICENSE).

Every bundled dependency is permissive (Apache-2.0 / MIT / BSD / ISC / PSF) or weak-copyleft
file-level (MPL-2.0, certifi), all compatible with the repository's AGPL-3.0 license. Their
required attributions are reproduced in NOTICE, and each installed package retains its own
license metadata inside the image.

## License

This image is licensed under the GNU Affero General Public License v3.0 (see LICENSE),
consistent with the percona-cd-platform repository it lives in. The only first-party code is
`reconcile.py`; everything else is an unmodified upstream dependency redistributed under its
own license (permissive, or weak file-level copyleft for certifi's MPL-2.0), as attributed in
NOTICE.

## Build (multi-arch)

EKS nodes are arm64 Graviton; CI builds this multi-arch on push to `main`:

```sh
docker buildx build --platform linux/amd64,linux/arm64 \
  -t <account>.dkr.ecr.us-east-1.amazonaws.com/percona-cd/jenkins-endpoint-reconciler:0.1.0 \
  --push images/jenkins-endpoint-reconciler
```

Refresh the base digest pin with:

```sh
docker buildx imagetools inspect ghcr.io/astral-sh/uv:python3.13-bookworm-slim \
  --format '{{.Manifest.Digest}}'
```

## Deploy

Pushed to ECR under `percona-cd/` and run by the reconciler CronJob in the
`resources/addons/jenkins-endpoint-reconciler` Helm chart (ArgoCD-managed). The container runs
as non-root (UID 65532) with dependencies baked in (no runtime `uv install`). Configure it via
`TARGET_NAMESPACE` and `HOSTS_JSON`; in-cluster credentials are read from the pod's
ServiceAccount (IRSA for EC2, the in-cluster config for the Kubernetes API).
