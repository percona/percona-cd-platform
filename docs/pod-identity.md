# Pod Identity

How in-cluster workloads get AWS credentials. EKS Pod Identity is the only
credential path for pods: IRSA was retired outright (no cluster OIDC
provider exists), and IMDSv2 hop limit 1 on every node blocks pods from
borrowing node credentials. Decision record:
[ADR 0004](adr/0004-pod-identity-default.md). Upstream reference:
[EKS Pod Identity](https://docs.aws.amazon.com/eks/latest/userguide/pod-identities.html)
and [how it works](https://docs.aws.amazon.com/eks/latest/userguide/pod-id-how-it-works.html).

## The pattern

- The `eks-pod-identity-agent` EKS addon (managed in
  `terraform/eks-addons.tf`) is the prerequisite. Without it every
  association silently no-ops, which is why the EBS CSI addon and the
  ArgoCD install both depend on it explicitly.
- Associations are created through the upstream
  `terraform-aws-modules/eks-pod-identity` module, one block per consumer
  in `terraform/pod-identity.tf`. No raw association resources exist.
- Binding is on the triple (cluster, namespace, service account), never on
  a role name. The namespace must match the addon directory's basename,
  because that is the namespace the ApplicationSet deploys into. A wrong
  namespace does not error, the workload just falls back to no credentials.
- Trust policies are delegated entirely to the upstream module (the
  `pods.eks.amazonaws.com` principal). Nothing hand-written exists in the
  repo.

## Association inventory

| Consumer (namespace / SA) | Role suffix | Grants |
|---|---|---|
| aws-load-balancer-controller / aws-load-balancer-controller | alb-controller | Upstream LB controller preset |
| external-dns / external-dns | external-dns | Route53, scoped to the one `cd.percona.com` zone |
| kube-system / ebs-csi-controller-sa | ebs-csi | EBS CSI preset plus the cluster KMS key |
| external-secrets / external-secrets | external-secrets | ESO preset plus a gap policy: the preset grants only `kms:Decrypt`, so a hand-rolled policy adds Get/Describe/List on `percona-ci-platform/*` secrets |
| mimir / mimir | mimir | Own S3 bucket plus the shared LGTM KMS key |
| loki / loki | loki | Same shape, own bucket |
| tempo / tempo | tempo | Same shape, own bucket |
| jenkins-endpoint-reconciler / jenkins-endpoint-reconciler | jenkins-discovery | Read-only `ec2:DescribeInstances` on `*` (the action supports no resource scoping, and masters span five regions) |
| jenkins-ps3-k8s / jenkins | jenkins-ctlr | The EC2-plugin provision surface, ASG control scoped to `jenkins-*-arm-*`, and `iam:PassRole` to worker roles |
| karpenter / karpenter | (inline via the karpenter submodule) | Karpenter v1 controller policy |

Role ARNs are exported from `terraform/outputs.tf` and reach charts through
the cluster-secret annotations
([`argocd-bootstrap.md`](argocd-bootstrap.md)).

## Adding an association

One module block in `terraform/pod-identity.tf`: prefer the module's
built-in `attach_<addon>_policy` preset, reserve hand-rolled policies for
gaps the presets do not cover (the ESO Secrets Manager gap is the model).
Then, if the chart needs the role ARN, add an output and a cluster-secret
annotation. Two files, three edits, no chart changes.

## What does not use Pod Identity

- The EC2 Jenkins masters and their workers run on instance profiles
  (`terraform/modules/jenkins-master`, the arm-fleet and arm-standalone
  modules). Their EC2-plugin credential resolution was solved in the
  patched plugin fork, not by Pod Identity.
- The Packer AMI factory builder uses its own instance profile
  (`terraform/iam-gha-ppg-ami-factory.tf`).
- cert-manager has no association because it is deliberately absent
  ([ADR 0007](adr/0007-cert-manager-deferred.md)).

The in-cluster ps3 controller is the deliberate crossover: it is a Jenkins
master that drives EC2 and ASG worker fleets purely through Pod Identity,
which is the credential model every controller inherits as masters move
in-cluster.
