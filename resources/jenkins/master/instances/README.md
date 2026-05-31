# Jenkins master instances

Each subdirectory here is one in-cluster Jenkins master. The `jenkins-masters`
ApplicationSet (`argocd-bootstrap/applicationsets/jenkins-app.yaml`) globs
`instances/*` and generates one Application `jenkins-<dir>` per subdirectory,
layering `instances/<dir>/values.yaml` over `../values-base.yaml`.

There are intentionally none right now: the only prior instance (`ps3-k8s`) was a
broken dark replica and is parked in `../../_disabled/ps3-k8s/`. Adding a master
here generates an Application that is **manual-sync** (see
[ADR 0025](../../../../docs/adr/0025-singleton-controller-rollout-gating.md)); it
will not auto-roll a singleton controller. Rebuild a pilot correctly before
adding it back.
