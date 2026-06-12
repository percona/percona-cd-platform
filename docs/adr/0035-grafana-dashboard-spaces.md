<!-- Copyright (C) 2026 Percona LLC -->
# 0035 — Grafana dashboard spaces: audience-aligned folder taxonomy

**Status:** Accepted (2026-06-12)
**Related:** [ADR 0016](0016-lgtm-only-metrics-stack.md) (datasource UID pinning), [ADR 0012](0012-authentik-saml-oidc-bridge.md) (group → role mapping), [ADR 0031](0031-in-cluster-synthetic-probing-for-jenkins-masters.md) (jenkins-uptime dashboard), [ADR 0034](0034-cloudwatch-exporter-for-aws-lb-metrics.md) (AWS LB dashboard)

## Context

grafana.cd.percona.com serves 43 repo-owned dashboards. The folder layout grew
out of implementation history, one folder per provisioning mechanism or
technology: `Kubernetes Mixin`, `Mimir Mixin`, `Loki`, `Karpenter`, `AWS`,
plus two empty `Platform / *` folders left behind by the retired gnetId
download path. Folder names answered "where did this JSON come from", not
"who is this for".

Folders are Grafana's permission boundary. The intent is to later restrict
spaces to authenticated groups (Duo/FreeIPA groups bridged through Authentik,
[ADR 0012](0012-authentik-saml-oidc-bridge.md)), so the taxonomy must be
audience-first. Constraints that shape it:

- Sidecar folder names must be single-segment. Slashes in `grafana_folder`
  annotations combined with `foldersFromFilesStructure` caused the PR #63/#64
  folder-explosion incident (12k empty folders); the lesson is recorded in
  values.yaml.
- Grafana OSS has no OIDC team sync (Enterprise feature). Folder ACLs will be
  teams plus folder permissions managed as code through the Grafana API or the
  Terraform provider. That requires a pinned admin credential
  (`admin.existingSecret` via ESO), which is a named follow-up; the live
  chart-generated admin password has rotated past the persisted value.
- Only two groups exist today: `grafana_cd_admins` (GrafanaAdmin) and
  `percona` (Viewer, all Perconians). Groups are provisioned upstream in
  FreeIPA/Duo by IT-Ops, not in this repo; the `grafana_cd_users` group named
  in [ADR 0010](0010-distributed-lgtm.md) was never created. A platform-tier
  group is an IT-Ops request, not a code change.

## Decision

Five top-level spaces, each mapping to one prospective permission tier:

| Space         | Dashboards | Audience (future ACL)                          |
|---------------|-----------:|------------------------------------------------|
| Jenkins       | 3          | Broad: CI health for all Percona viewers        |
| MTR           | 2          | Broad: QA and product developers                |
| Kubernetes    | 14         | Platform tier (kubernetes-mixin set)            |
| Observability | 22         | Platform tier (Mimir mixin, Loki, future Tempo) |
| Platform      | 2          | Platform tier (AWS LB, Karpenter; future cost)  |

`General` stays empty. Two ACL tiers when groups land: Jenkins and MTR remain
org-wide Viewer; Kubernetes, Observability, and Platform get restricted to the
platform group.

Dashboard titles follow `Domain / Thing` (`Jenkins / Uptime`,
`MTR / Test History`, `AWS / Load Balancers`), matching the upstream mixin
convention (`Mimir / ...`, `Kubernetes / ...`). Titles say what the dashboard
shows, not which component emits the metrics (`Jenkins / Hetzner Workers`,
formerly "Hetzner Cloud Plugin"). UIDs never change on rename or move, so
links and bookmarks survive.

Provisioning is sidecar-only: one ConfigMap per dashboard (or per mixin file)
with the `grafana_dashboard` label and a single-segment `grafana_folder`
annotation. The legacy file-based `dashboardProviders` (platform-lgtm,
platform-karpenter, jenkins) are removed; they provisioned nothing since the
gnetId retirement and only kept empty folders alive.

## Consequences

- Adding a dashboard means choosing one of the five spaces (or making the
  case for a new space in an ADR update); the annotation is the only wiring.
- Folder moves are pure annotation edits; the provisioner re-folders
  dashboards on the next sidecar sync with no UID churn.
- Empty folder rows are not garbage-collected by provisioning. The
  reorganization leaves seven empty folders (AWS, Karpenter, Loki,
  Kubernetes Mixin, Mimir Mixin, Platform / LGTM, Platform / Karpenter) to be
  deleted once via the UI or, after the admin-credential pin lands, the
  folders API (precedent: the combined `LGTM + Karpenter` folder, API-deleted
  2026-05-09).
- The permissions phase has two prerequisites before any ACL code: the
  ESO-pinned admin credential, and an IT-Ops request for a platform-tier
  group. Until then every space remains org-wide Viewer.
