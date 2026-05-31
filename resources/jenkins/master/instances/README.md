# Jenkins master instances

Each subdirectory here is one in-cluster Jenkins master. The `jenkins-masters`
ApplicationSet (`argocd-bootstrap/applicationsets/jenkins-app.yaml`) globs
`instances/*` and generates one Application `jenkins-<dir>` per subdirectory,
layering `instances/<dir>/values.yaml` over `../values-base.yaml`.

`ps3-k8s` is the first pilot (a fresh-PVC dark replica; production `ps3.cd` stays
on EC2). Generated Applications are **manual-sync** (see
[ADR 0025](../../../../docs/adr/0025-singleton-controller-rollout-gating.md)); they
do not auto-roll a singleton controller. Park a master under `../../_disabled/` to
take it out of the generator.
