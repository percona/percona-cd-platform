# Disabled Jenkins master instances

Instance value files parked here are intentionally OUT of the `jenkins-masters`
ApplicationSet generator (`resources/jenkins/master/instances/*`), so ArgoCD
generates no Application for them and prunes any that were live.

None right now: `ps3-k8s` was re-enabled under `instances/ps3-k8s/` on 2026-05-31
once its prerequisites landed (the chart consumes values, a controller image is in
ECR, and the substrate node pool + Pod Identity + Retain PVC exist). Park a value
file here to take a master out of the generator without deleting it.
