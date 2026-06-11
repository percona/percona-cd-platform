<!-- Copyright (C) 2026 Percona LLC -->
# 0033 — S3 gateway endpoint policy: allow all S3, govern at IAM

**Status:** Accepted (2026-06-11)
**Related:** [ADR 0011](0011-robustness-pass.md) (the robustness pass that added the VPC endpoints), [ADR 0019](0019-shared-alb-ssl-termination-for-jenkins-masters.md) (the per-master TF VPCs these endpoints live in).

## Context

Each TF-managed master VPC (`terraform/modules/jenkins-master`) provisions an S3 **Gateway** VPC endpoint to keep same-region S3 traffic off NAT. A gateway endpoint policy is a filter on every S3 request leaving the VPC: effective access is the intersection of the endpoint policy and IAM, and the endpoint intercepts **all** same-region S3 traffic whether or not a workload knows the endpoint exists.

The endpoint policy was scoped by **action** to seven object/list actions (`ListBucket`, `GetObject`, `GetObjectAcl`, `PutObject`, `PutObjectAcl`, `DeleteObject`, `AbortMultipartUpload`). It omitted version-level and bucket-level actions.

That silently broke `openshift-cluster-destroy`: its Jenkins worker runs in the pmm master VPC, and `openshift-install destroy` needs `s3:ListBucketVersions` to empty the cluster's versioned image-registry bucket. The endpoint denied it ("no VPC endpoint policy allows the `s3:ListBucketVersions` action"), so the destroy retried forever and OpenShift clusters leaked. IAM (`percona-openshift-user`) allowed the action; the endpoint did not. Because the module is shared, every migrated master VPC carries the same gap.

## Decision

- Set the S3 gateway endpoint policy to allow **all S3 actions** (`s3:*` on `*`, principal `*`). Access control is delegated to IAM, which already scopes each principal (master role, worker roles, `percona-openshift-user`). This matches AWS's default gateway-endpoint posture.
- Keep `Resource = "*"` deliberately: these CI workers legitimately read **cross-account public** S3 (AL2023 `dnf` mirrors, OpenShift / Red Hat release content, public download buckets). An `aws:ResourceAccount` / `aws:ResourceOrgID` restriction would break those.

## Consequences

- Any worker S3 operation that IAM permits now works through the endpoint, including version and bucket-level ops, so `openshift-cluster-destroy` can complete.
- The endpoint no longer provides action- or resource-level exfiltration hardening; that is delegated to IAM (and, if ever needed, SCPs). This is the conventional posture for a heterogeneous CI VPC, not the most locked-down option.
- Applies to all eight TF master VPCs (pmm, psmdb, ps80, ps57, pxb, pxc, rel, cloud) via the shared module. The change is an **in-place** endpoint policy update, not a VPC or endpoint replacement. pg (CloudFormation) and ps3 (in-cluster) are unaffected.

## Alternatives considered

- **Action-scoped (the status quo)** — rejected. Filtering by action at a gateway endpoint buys little security (a principal with `GetObject`/`PutObject` on `*` can already exfiltrate) while guaranteeing silent VPC-wide breakage of any workload that needs another action.
- **Resource/org-scoped (`aws:ResourceOrgID` / `aws:ResourceAccount`)** — the textbook anti-exfiltration hardening, deferred. It would block the legitimate cross-account public reads these workers depend on (package mirrors, release images). Revisit only with explicit bucket carve-outs for those sources.
- **Add only the missing OpenShift actions** (`ListBucketVersions`, `DeleteObjectVersion`, `GetBucketVersioning`, ...) — rejected as brittle: the next workload needing a different action breaks again.
