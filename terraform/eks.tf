# Owner: platform
# EKS control plane + three managed node groups (system, prometheus_system,
# jenkins_master).
# Karpenter handles workload nodes; managed NGs host stateful + bootstrap pods only.
# jenkins_system NG was removed 2026-05-13: no Jenkins master pod ever claimed
# its taint, so the m6a.xlarge was running only DaemonSets at ~$126/mo. It is
# re-added below as jenkins_master for the in-cluster controller pilot (ps3-k8s),
# which DOES claim the tier via the chart's nodeSelector + toleration.
#
# Hardening baked in (see docs/eks-hardening.md):
#   1. authentication_mode = "API" + enable_cluster_creator_admin_permissions = false
#      → cluster admin granted via the committed sso_admin baseline below
#      (dynamic IAM lookup, no ARN literals) merged with var.access_entries
#      overrides (no implicit creator grant).
#   2. endpoint_public_access_cidrs from the SSM-backed allowlist
#      (allowlists.tf; no public-unrestricted).
#   3. enabled_log_types = ["audit", "authenticator", "api"] → CloudWatch.
#   9. create_kms_key = true → customer-managed CMK for envelope encryption.
#  10. metadata_options on each NG → IMDSv2 required, hop-limit 1 (Pod Identity removes
#      any need for hop=2; pods get IAM via the agent, not the node IMDS).

# Resolves the IAM Identity Center AdministratorAccess permission-set role at
# plan time, so the baseline cluster-admin entry below carries no principal
# ARN literal (the repo is public). If Identity Center re-provisions the
# permission set (new role-name suffix), the next plan resolves the new ARN
# automatically and replaces the entry. The postcondition requires exactly one
# match and fails the plan on zero or several.
data "aws_iam_roles" "sso_admin" {
  name_regex  = "AWSReservedSSO_AdministratorAccess_.*"
  path_prefix = "/aws-reserved/sso.amazonaws.com/"

  lifecycle {
    postcondition {
      condition     = length(self.arns) == 1
      error_message = "Expected exactly one AWSReservedSSO_AdministratorAccess_* role under /aws-reserved/sso.amazonaws.com/; found zero or several. Clean up IAM Identity Center provisioning or tighten the regex before applying."
    }
  }
}

locals {
  # Baseline access entries, always present. var.access_entries extends the
  # map; on key collision the tfvars-supplied entry wins (merge order).
  base_access_entries = {
    sso_admin = {
      principal_arn = one(data.aws_iam_roles.sso_admin.arns)
      policy_associations = {
        admin = {
          policy_arn   = "arn:aws:eks::aws:cluster-access-policy/AmazonEKSClusterAdminPolicy"
          access_scope = { type = "cluster" }
        }
      }
    }
  }
}

module "eks" {
  source  = local.modules.eks.source
  version = local.modules.eks.version

  name               = local.cluster_name
  kubernetes_version = var.cluster_version

  vpc_id     = module.vpc.vpc_id
  subnet_ids = module.vpc.private_subnets

  # Hardening #1 — access entries are the only path to cluster admin. The
  # committed sso_admin baseline (above) is merged with tfvars overrides.
  authentication_mode                      = "API"
  enable_cluster_creator_admin_permissions = false
  access_entries                           = merge(local.base_access_entries, var.access_entries)

  # Hardening #2 — public endpoint allowlisted; private endpoint also enabled so
  # in-VPC traffic never leaves AWS. The baseline allowlist is SSM-resolved
  # (allowlists.tf); var.api_public_access_cidrs adds emergency overrides only.
  endpoint_public_access       = true
  endpoint_private_access      = true
  endpoint_public_access_cidrs = local.eks_api_allowed_cidrs

  # Hardening #3 — control-plane logging.
  enabled_log_types = ["audit", "authenticator", "api"]

  # Hardening #9 — envelope encryption with a customer-managed CMK that the module
  # creates alongside the cluster and rotates yearly.
  create_kms_key                  = true
  enable_kms_key_rotation         = true
  kms_key_deletion_window_in_days = 7
  encryption_config = {
    resources = ["secrets"]
  }

  # Managed addons land in eks-addons.tf (kept separate for clearer Pod Identity wiring).
  addons = {}

  # All workloads authenticate to AWS via Pod Identity (see pod-identity.tf),
  # not IRSA. Disabling enable_irsa removes the OIDC provider that nothing
  # consumes; the provider itself was already deleted out-of-band, so this
  # is a state-only change.
  enable_irsa = false

  eks_managed_node_groups = {
    system = {
      instance_types = local.ng.system.instance_types
      capacity_type  = "ON_DEMAND"
      min_size       = local.ng.system.min_size
      desired_size   = local.ng.system.desired_size
      max_size       = local.ng.system.max_size
      # Pin the AMI release so unrelated tofu applies don't trigger a rolling
      # drain. Bump explicitly when an upgrade is intentional.
      ami_release_version            = "1.35.4-20260505"
      use_latest_ami_release_version = false
      # Tier taxonomy: `workload.percona.com/tier=bootstrap` is the
      # canonical label. The CriticalAddonsOnly taint paired with this
      # label makes the system MNG exclusive -- bootstrap-tier addons
      # (karpenter, external-secrets, AWS LB controller, ArgoCD,
      # external-dns, kube-state-metrics) tolerate it explicitly;
      # everything else gets routed to the `default` Karpenter NodePool.
      # AWS-canonical taint key per
      # https://docs.aws.amazon.com/eks/latest/userguide/critical-workload.html.
      labels = {
        "workload.percona.com/tier"       = "bootstrap"
        "workload.percona.com/managed-by" = "mng"
      }
      taints = {
        critical_addons_only = {
          key    = "CriticalAddonsOnly"
          value  = "true"
          effect = "NO_SCHEDULE"
        }
      }

      # Hardening #10 — IMDSv2 required, hop-limit 1.
      metadata_options = {
        http_tokens                 = "required"
        http_put_response_hop_limit = 1
        http_endpoint               = "enabled"
      }
    }

    prometheus_system = {
      instance_types = local.ng.prometheus_system.instance_types
      capacity_type  = "ON_DEMAND"
      min_size       = local.ng.prometheus_system.min_size
      desired_size   = local.ng.prometheus_system.desired_size
      max_size       = local.ng.prometheus_system.max_size
      subnet_ids     = [module.vpc.private_subnets[0]] # var.monitoring_az pinned (us-east-1a)
      # Pin the AMI release; Authentik PG / Grafana / MTR-PG PDBs block evictions
      # during a rolling drain, so version bumps must be explicit. With max_size=2
      # the MNG rolling update surges a fresh node before draining the old (the
      # landing spot for the evicted stateful singles), and force_update_version
      # lets the drain proceed past the zero-disruption PDBs
      # (mtr-pg-primary ALLOWED=0, single-replica authentik-postgresql, Grafana
      # minAvailable=1 with both replicas co-located). See
      # docs/runbooks/mng-label-taint-changes.md: AMI bumps are the case where
      # rolling IS intended; pair with force.
      ami_release_version            = "1.35.5-20260520"
      use_latest_ami_release_version = false
      force_update_version           = true
      # Tier taxonomy: `workload.percona.com/tier=obs-state`. Legacy
      # `workload=prometheus` / `node-role=stateful` keys + taint were
      # dropped after consumers (Grafana, Authentik) migrated to the
      # canonical tier key.
      labels = {
        "workload.percona.com/tier"       = "obs-state"
        "workload.percona.com/managed-by" = "mng"
      }
      taints = {
        tier = {
          key    = "workload.percona.com/tier"
          value  = "obs-state"
          effect = "NO_SCHEDULE"
        }
      }
      metadata_options = {
        http_tokens                 = "required"
        http_put_response_hop_limit = 1
        http_endpoint               = "enabled"
      }
    }

    # In-cluster Jenkins controller pilot (ps3-k8s). Re-adds a dedicated node
    # pool in the spirit of the jenkins_system NG removed 2026-05-13, but this
    # time a pod WILL claim it: the controller chart's nodeSelector + toleration
    # target `workload.percona.com/tier=jenkins-master`. Single-AZ (us-east-1a)
    # so it co-locates with the gp3-jenkins-1a-retain PVC (EBS cannot cross AZ).
    # Managed NG (not Karpenter), so consolidation never disrupts it; pinned AMI
    # + single node (min=desired=max=1) keep it stable for the SPOF controller.
    jenkins_master = {
      instance_types = local.ng.jenkins_master.instance_types
      capacity_type  = "ON_DEMAND"
      min_size       = local.ng.jenkins_master.min_size
      desired_size   = local.ng.jenkins_master.desired_size
      max_size       = local.ng.jenkins_master.max_size
      subnet_ids     = [module.vpc.private_subnets[0]] # us-east-1a; matches gp3-jenkins-1a-retain

      # Pin the AMI release so an unrelated tofu apply never rolls (drains) the
      # singleton controller node. Bump explicitly in a maintenance window.
      ami_release_version            = "1.35.5-20260520"
      use_latest_ami_release_version = false

      labels = {
        "workload.percona.com/tier"       = "jenkins-master"
        "workload.percona.com/managed-by" = "mng"
      }
      taints = {
        tier = {
          key    = "workload.percona.com/tier"
          value  = "jenkins-master"
          effect = "NO_SCHEDULE"
        }
      }

      metadata_options = {
        http_tokens                 = "required"
        http_put_response_hop_limit = 1
        http_endpoint               = "enabled"
      }
    }

  }

  # Karpenter discovers the SGs to attach to its launched nodes via the
  # `karpenter.sh/discovery=<cluster_name>` tag (see
  # resources/addons/karpenter/templates/ec2nodeclass-default.yaml). The
  # private subnets carry it via vpc.tf; tag both the module-managed
  # cluster SG and node SG here so the tag is durable. Without these,
  # Karpenter logs `SecurityGroupSelector did not match any
  # SecurityGroups` and never provisions a node, even though the
  # NodePool/EC2NodeClass are otherwise ready.
  security_group_tags = {
    "karpenter.sh/discovery" = local.cluster_name
  }
  node_security_group_tags = {
    "karpenter.sh/discovery" = local.cluster_name
  }

  tags = local.tags
}
