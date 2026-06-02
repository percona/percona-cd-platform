# Jenkins master instances

Each subdirectory here is one in-cluster Jenkins master. The `jenkins-masters`
ApplicationSet (`argocd-bootstrap/applicationsets/jenkins-app.yaml`) globs
`instances/*` and generates one Application `jenkins-<dir>` per subdirectory,
layering `instances/<dir>/values.yaml` over `../values-base.yaml`.

`ps3-k8s` is the first pilot. It began as a fresh-PVC dark replica, then was cut over to
serve `ps3.cd` directly from the in-cluster controller (the EC2 proxy was retired); the
other nine masters remain on EC2. Generated Applications are **manual-sync** (see
[ADR 0025](../../../../docs/adr/0025-singleton-controller-rollout-gating.md)); they
do not auto-roll a singleton controller. Park a master under `../../_disabled/` to
take it out of the generator.
