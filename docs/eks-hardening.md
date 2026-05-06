# EKS hardening checklist

Gap analysis from a 2026 audit of the platform plan against the [AWS EKS
Best Practices Guide](https://docs.aws.amazon.com/eks/latest/best-practices/).
Each row links to the upstream doc. This file is the source of truth for
"what we said we would do but haven't wired in yet" — the skeleton
`terraform/*.tf` files carry pointer comments back here so nothing slips
through at uncomment time.

## Top 5 — must land before merging modules

| # | Item | Where | Source |
|---|---|---|---|
| 1 | **Access entries with `authenticationMode=API`**; `bootstrapClusterCreatorAdminPermissions=false`; explicit access entries for human operators | `terraform/eks.tf` (`access_entries`, `authentication_mode`) | [IAM best practices](https://docs.aws.amazon.com/eks/latest/best-practices/identity-and-access-management.html) |
| 2 | **`publicAccessCidrs` allowlist** on the API endpoint (Percona office / VPN CIDRs) | `terraform/eks.tf` (`cluster_endpoint_public_access_cidrs`) | [Cluster endpoint access](https://docs.aws.amazon.com/eks/latest/userguide/cluster-endpoint.html) |
| 3 | **Control-plane logging** — at minimum `audit` + `authenticator` to CloudWatch | `terraform/eks.tf` (`cluster_enabled_log_types`) | [Control-plane logs](https://docs.aws.amazon.com/eks/latest/userguide/control-plane-logs.html) |
| 4 | **VPC CNI prefix delegation** (`ENABLE_PREFIX_DELEGATION=true`) — 4× pod density on m6a.xlarge | `terraform/eks-addons.tf` (`aws_eks_addon "vpc-cni" { configuration_values = ... }`) | [Prefix mode for Linux](https://docs.aws.amazon.com/eks/latest/best-practices/prefix-mode-linux.html) |
| 5 | **Pin every managed-addon version** + Karpenter AMI alias — no `@latest` anywhere | `terraform/eks-addons.tf` (`addon_version`), `resources/addons/karpenter/nodepools/ec2nodeclass.yaml` (`amiSelectorTerms.alias`) | [Cluster upgrades](https://docs.aws.amazon.com/eks/latest/best-practices/cluster-upgrades.html) |

## Follow-up backlog (post-merge, pre-prod)

| # | Item | Where | Source |
|---|---|---|---|
| 6 | **PodDisruptionBudgets** for kube-prometheus-stack, ArgoCD HA, AWS LBC, external-dns, ps3-k8s StatefulSet | per-addon `values.yaml` | [Reliability](https://docs.aws.amazon.com/eks/latest/best-practices/reliability.html) |
| 7 | **PriorityClasses** — `system-cluster-critical` for system addons; custom high priority for stateful workloads so Karpenter doesn't preempt | per-workload manifest | [Reliability](https://docs.aws.amazon.com/eks/latest/best-practices/reliability.html) |
| 8 | **PSA labels** (`pod-security.kubernetes.io/{enforce,audit,warn}=restricted`) on `monitoring`, `jenkins-system`, `jenkins-ps3-k8s`. Start in `audit/warn`, promote after soak. | namespace manifests | [Pod security](https://docs.aws.amazon.com/eks/latest/best-practices/pod-security.html) |
| 9 | **Customer-managed KMS CMK** for cluster secrets envelope encryption + EBS volume encryption | `terraform/kms.tf` (new), `terraform/eks.tf` (`encryption_config`), StorageClass `parameters.kmsKeyId` | [Envelope encryption](https://docs.aws.amazon.com/eks/latest/userguide/envelope-encryption.html) |
| 10 | **IMDSv2 enforcement + hop-limit=1** on worker nodes (Pod Identity removes the need for hop=2) | EKS managed-NG launch template (`metadata_options`) | [Restrict node IMDS](https://kubedemy.io/aws-eks-part-15-restrict-node-imds-to-secure-aws-account-access) |
| 11 | **S3 gateway VPC endpoint** (free); revisit STS / ECR / Secrets Manager interface endpoints when NAT-GW bill warrants | `terraform/vpc.tf` (`enable_s3_endpoint`) | [Cost-opt networking](https://docs.aws.amazon.com/eks/latest/best-practices/cost-opt-networking.html) |
| 12 | **~~fluent-bit DaemonSet → CloudWatch Logs~~** — superseded by Alloy DaemonSet → Loki ([ADR 0010](adr/0010-distributed-lgtm.md)). |  | |
| 13 | **Karpenter** `consolidationPolicy: WhenEmptyOrUnderutilized`, `disruption.budgets`, `expireAfter: 720h`, NodePool `limits.cpu`. Annotate long Jenkins build pods with `karpenter.sh/do-not-disrupt`. | `resources/addons/karpenter/nodepools/default.yaml` | [Karpenter](https://docs.aws.amazon.com/eks/latest/best-practices/karpenter.html) |
| 14 | **VolumeSnapshotClass** for EBS CSI (independent of AWS Backup; useful for Velero / ad-hoc) | new manifest under `resources/addons/storageclass-gp3/templates/` | [EBS CSI driver](https://github.com/kubernetes-sigs/aws-ebs-csi-driver/blob/master/docs/install.md) |
| 15 | **Alertmanager routing** — Slack `#opensource-jenkins` / PagerDuty (chart ships empty config) | `resources/addons/kube-prometheus-stack/values.yaml` (`alertmanager.config`) | upstream chart |
| 16 | **VPC CNI native NetworkPolicy** default-deny baseline; revisit Cilium chaining if L7/FQDN ever needed | `terraform/eks-addons.tf` (`vpc-cni` `configuration_values.enableNetworkPolicy=true`), per-namespace `NetworkPolicy` | [Network Policy engine](https://aws.amazon.com/blogs/containers/rippling-vpc-cni-network-policy-engine/) |
| 17 | **VPA recommender mode** (no auto-update) for sizing the NGINX reverse-proxy Deployments | new addon `resources/addons/vpa/` (recommender only) | [Vertical Pod Autoscaler](https://github.com/kubernetes/autoscaler/tree/master/vertical-pod-autoscaler) |
| 18 | **Bearer-token auth on Alloy gateway** (NGINX sidecar validating `Authorization` header against ESO-synced Secrets Manager value) — current v1 auth is ALB CIDR allowlist only | `resources/addons/alloy-gateway/` (new sidecar pattern) | [Alloy auth components](https://grafana.com/docs/alloy/latest/reference/components/) |
| 19 | **S3 cross-region replication** for the three LGTM buckets (mimir-blocks, loki-chunks, tempo-traces) — replica bucket in `us-west-2` or `eu-central-1` | `terraform/lgtm-storage.tf` (`aws_s3_bucket_replication_configuration`) | [Replication](https://docs.aws.amazon.com/AmazonS3/latest/userguide/replication.html) |
| 20 | **S3 Object Lock (Compliance, 7–14 d)** on the LGTM buckets to block ransomware-style mass-delete | `terraform/lgtm-storage.tf` (`object_lock_enabled`) | [Object Lock](https://docs.aws.amazon.com/AmazonS3/latest/userguide/object-lock.html) |
| 21 | **SAML cert rotation tracking** for Grafana — Alertmanager rule on cert expiry, calendar reminder 30 d before, runbook in `docs/runbooks/grafana-saml-cutover.md` | `resources/addons/kube-prometheus-stack/` (PrometheusRule), runbook | upstream Grafana SAML |

## Explicitly deferred (not gaps — documented choices)

- **EKS Auto Mode** — would minimise ops burden but locks us into AWS-managed Karpenter, AWS-managed addon versions, and a fixed NodePool shape. Our AZ-pinned stateful workloads, custom taints, and explicit Karpenter spot/on-demand mix justify standard EKS. Revisit when (a) the platform is steady-state and (b) Auto Mode supports per-NodePool taints we control.
- **Multi-AZ NAT-GW** — single NAT-GW saves cost in v1. Document in a follow-up ADR; revisit when cross-AZ data-transfer charges become material.
- **AZ-pinned stateful workloads (Prometheus, Jenkins masters)** — accepted SPOF. Multi-AZ HA needs EFS (slower for fsync-heavy Jenkins) or leader-election (overkill at this scale). Captured in [ADR 0008](adr/0008-managed-ng-for-stateful-system-workloads.md).
- **EKS Hybrid Nodes** — pure cloud, not applicable.
- **IPv6 cluster mode** — `10.220.0.0/16` has plenty of address space.
- **cert-manager** — deferred to v1.5 per [ADR 0007](adr/0007-cert-manager-deferred.md).
- ~~**LGTM (Mimir / Tempo / Loki)** — deferred per ADR 0006~~ — adopted in distributed mode per [ADR 0010](adr/0010-distributed-lgtm.md). Items 18–21 above carry the post-adoption hardening backlog.

## Source-document index

- [EKS Best Practices Guide (root)](https://docs.aws.amazon.com/eks/latest/best-practices/)
- [Security](https://docs.aws.amazon.com/eks/latest/best-practices/security.html)
- [Identity & access management](https://docs.aws.amazon.com/eks/latest/best-practices/identity-and-access-management.html)
- [Networking](https://docs.aws.amazon.com/eks/latest/best-practices/networking.html)
- [Pod security](https://docs.aws.amazon.com/eks/latest/best-practices/pod-security.html)
- [Reliability](https://docs.aws.amazon.com/eks/latest/best-practices/reliability.html)
- [Karpenter](https://docs.aws.amazon.com/eks/latest/best-practices/karpenter.html)
- [Cluster upgrades](https://docs.aws.amazon.com/eks/latest/best-practices/cluster-upgrades.html)
- [Cost-opt networking](https://docs.aws.amazon.com/eks/latest/best-practices/cost-opt-networking.html)
- [Envelope encryption (1.28+)](https://docs.aws.amazon.com/eks/latest/userguide/envelope-encryption.html)
- [Control-plane logs](https://docs.aws.amazon.com/eks/latest/userguide/control-plane-logs.html)
- [Pod Identities](https://docs.aws.amazon.com/eks/latest/userguide/pod-identities.html)
- [EKS Auto Mode](https://docs.aws.amazon.com/eks/latest/best-practices/automode.html)
