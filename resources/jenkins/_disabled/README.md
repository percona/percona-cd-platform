# Disabled Jenkins master instances

Instance value files parked here are intentionally OUT of the `jenkins-masters`
ApplicationSet generator (`resources/jenkins/master/instances/*`), so ArgoCD
generates no Application for them and prunes any that were live.

## ps3-k8s

Disabled 2026-05-31. It was a broken dark replica: the umbrella chart never
delivered these values to the `jenkins` subchart, so it came up as the chart
default (8Gi `Delete` PVC, no custom image, ALB group `jenkins-shared`), STS
`0/1`, pod `Pending`. EC2 `ps3.cd` serves production throughout. See
[ADR 0025](../../../docs/adr/0025-singleton-controller-rollout-gating.md).

These values are kept for reference only. A real pilot must be rebuilt
correctly (values consumed by the subchart, a controller image, a Retain +
AZ-pinned PVC, a dedicated node pool) before being re-added under `instances/`.
