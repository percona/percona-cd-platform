# 0024 — Jenkins fleet ownership boundary: five-layer model

**Status:** Proposed (2026-05-31)
**Related:** [ADR 0005](0005-gitops-bridge-bootstrap.md) (GitOps-bridge bootstrap), [ADR 0008](0008-managed-ng-for-stateful-system-workloads.md) (managed NG for stateful workloads), [ADR 0013](0013-push-from-masters-with-nginx-bearer.md) (master-side push pipeline), [ADR 0019](0019-shared-alb-ssl-termination-for-jenkins-masters.md) (shared ALB front door), [ADR 0027](0027-baked-jenkins-controller-image.md) (the baked image that shrinks the EBS plugin residue this model calls for).

## Context

The ~10-master Jenkins fleet is moving onto a uniform substrate (the `jenkins-master` Terraform module + EKS-fronted ingress). As that work proceeds, the same artifact can plausibly be owned by more than one system: a master's DNS record could be a Terraform `aws_route53_record`, an external-dns annotation, or a manual entry; `init.groovy.d` cloud config could live in Terraform, in a Helm chart, or in Git; plugins could be installed at boot or baked into an image.

Without a written boundary, ownership drifts to whoever last touched the resource. The recent Terraform pains (shared-state cascade, `$Latest` launch-template drift, spurious replacement) were all state/IaC problems, and every one of them got worse when a second system also believed it owned the resource. The convergent finding of the fleet-management review (workflow + Codex + three opus red-teams) is that hosting OSS Jenkins in-cluster changes the supervisor but does not add HA; the real payoff is operational uniformity, GitOps, and a small, snapshot-backed runtime residue. That payoff only materializes if each artifact has exactly one owner.

This ADR records the five-layer ownership model so the boundary is durable and reviewable, and names the legitimate Terraform exceptions explicitly so a reviewer can tell a deliberate exception from a violation.

## Decision

Every fleet artifact belongs to exactly one of five layers (four control-plane owners plus the runtime-state data layer). An owner never reaches across the boundary into another layer's artifacts.

| Layer | Owner | Owns | Does NOT own |
|---|---|---|---|
| AWS substrate | Terraform (OpenTofu) | VPC / peering / SGs, EKS + node groups, IAM + Pod Identity, ACM, ECR, KMS, S3; per-master EC2 / spot-fleet; the EBS DATA volume (`prevent_destroy`) | anything inside the cluster, Jenkins app config, DNS records |
| In-cluster workloads | ArgoCD / Helm | addons, `jenkins-ingress` NGINX, EndpointSlice reconciler, Karpenter NodePools, FUTURE in-cluster controllers | the AWS substrate beneath them, Jenkins app config |
| Jenkins app config | JCasC + Git | clouds (EC2 / Hetzner), agent templates + labels, jobs, security realm; `init.groovy.d` *content* | the substrate, the delivery mechanism |
| Master DNS | external-dns | `*.cd` records (module sets `create_route53_record = false`) | anything else |
| Runtime state | EBS + Secrets Manager | build history, credentials, plugins (today); backed by snapshots | config that belongs in Git |

Governing principle: keep the irreplaceable EBS residue as small as possible. Plugins shift to a baked image, config to JCasC/Git, secrets to Secrets Manager, large artifacts to S3, so the volume converges to history-only.

If a state split is ever revisited (it is not now; see [ADR 0001](0001-opentofu-over-terraform.md) for the OpenTofu baseline and the reconciled plan's single-state decision), it is organizational only: file naming (`structural` + `ec2-masters`, never `masters`) and justfile targets. Moving a resource between `.tf` files in one root does not change its state address, so it needs no state surgery. Future in-cluster controllers belong to ArgoCD/Helm, never pushed into Terraform to win a split.

### Legitimate Terraform exceptions (deliberate, not violations)

Three places where Terraform legitimately touches a higher layer. These are bootstrap or substrate-delivery exceptions, scoped and named so review does not flag them as boundary breaks:

1. **ArgoCD bootstrap.** Terraform installs the ArgoCD control plane and the root/bootstrap Application (the GitOps-bridge handoff, [ADR 0005](0005-gitops-bridge-bootstrap.md)). This is the one-time seam that hands the in-cluster layer to ArgoCD. Terraform owns getting ArgoCD running; ArgoCD owns everything ArgoCD then syncs. Terraform must not manage individual addon Applications past the bootstrap root.

2. **The OIDC `ExternalSecret` wiring.** Terraform provisions the IAM + Secrets Manager substrate for SSO/OIDC and the `ExternalSecret`/cluster-secret plumbing that lets ESO resolve it. This is substrate (IAM, KMS, SM) plus the minimum cluster-side glue to make the secret reachable, not ongoing app config. The consuming workloads remain ArgoCD/Helm-owned.

3. **`init.groovy.d` S3 delivery.** Terraform uploads each master's `init.groovy.d` files to the per-master S3 bucket and grants the read. Terraform owns *delivery* (the S3 object + IAM grant, a substrate concern); JCasC + Git own the *content*. The boundary line is exact: a change to what a groovy script does is a Git change; a change to which bucket/key it lands in or who may read it is a Terraform change. (For an in-cluster controller the same content arrives as a ConfigMap via ArgoCD instead of S3, moving delivery to the in-cluster layer.)

Any Terraform reaching into a higher layer that is NOT one of these three is a boundary violation and should be rejected in review.

## Consequences

**(+) One owner per artifact.** A reviewer can answer "who owns this?" deterministically, which removes the drift class behind the worst recent Terraform incidents.

**(+) Shrinking blast radius.** As plugins/config/secrets leave EBS, a master becomes rebuildable from code plus a small snapshot. This de-risks EC2 today and is the prerequisite for any clean k8s move.

**(+) The exceptions are auditable.** Three named seams mean a `git grep` plus a code-review rule can enforce the boundary instead of relying on tribal memory.

**(-) Some friction at the seams.** A change that spans delivery and content (e.g. a new groovy script in a new bucket key) touches two layers in two PRs. This is intentional: it forces the delivery-vs-content split to stay clean.

**(-) The model is aspirational at the edges today.** Plugins still live on EBS until the baked image lands ([ADR 0027](0027-baked-jenkins-controller-image.md), now landing for the in-cluster pilot); some config is still groovy, not JCasC. The boundary describes the target; the reproducibility direction is what closes the gap.

## Verification

- `git grep` in `terraform/` for `aws_route53_record` scoped to a master host returns nothing; the module sets `create_route53_record = false` and external-dns owns `*.cd`.
- The only Terraform that references ArgoCD Applications is the bootstrap root (the GitOps-bridge handoff), not individual addons.
- Each master's `init.groovy.d` content lives in Git (`resources/jenkins-masters/<inst>/init.groovy.d/`); Terraform references it only as an S3 upload + IAM read grant.
- `just ci` passes (fmt, validate, trivy, kubeconform).
- A new resource added to any layer can be placed under exactly one owner in the table above; if it cannot, the boundary is wrong and the ADR is amended, not the boundary quietly crossed.
