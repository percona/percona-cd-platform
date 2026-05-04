# EKS control plane + three managed node groups (system, prometheus_system, jenkins_system).
# Karpenter handles workload nodes; managed NGs host stateful + bootstrap pods only.
#
# Hardening baked in (see docs/eks-hardening.md):
#   1. authentication_mode = "API" + enable_cluster_creator_admin_permissions = false
#      → cluster admin granted only via var.access_entries (no implicit creator grant).
#   2. endpoint_public_access_cidrs = var.api_public_access_cidrs (no public-unrestricted).
#   3. enabled_log_types = ["audit", "authenticator", "api"] → CloudWatch.
#   9. create_kms_key = true → customer-managed CMK for envelope encryption.
#  10. metadata_options on each NG → IMDSv2 required, hop-limit 1 (Pod Identity removes
#      any need for hop=2; pods get IAM via the agent, not the node IMDS).

module "eks" {
  source  = local.modules.eks.source
  version = local.modules.eks.version

  name               = local.cluster_name
  kubernetes_version = var.cluster_version

  vpc_id     = module.vpc.vpc_id
  subnet_ids = module.vpc.private_subnets

  # Hardening #1 — access entries are the only path to cluster admin.
  authentication_mode                      = "API"
  enable_cluster_creator_admin_permissions = false
  access_entries                           = var.access_entries

  # Hardening #2 — public endpoint allowlisted; private endpoint also enabled so
  # in-VPC traffic never leaves AWS. var.api_public_access_cidrs is required (no default).
  endpoint_public_access       = true
  endpoint_private_access      = true
  endpoint_public_access_cidrs = var.api_public_access_cidrs

  # Hardening #3 — control-plane logging.
  enabled_log_types = ["audit", "authenticator", "api"]

  # Hardening #9 — envelope encryption with a customer-managed CMK that the module
  # creates on our behalf and rotates yearly.
  create_kms_key                  = true
  enable_kms_key_rotation         = true
  kms_key_deletion_window_in_days = 7
  encryption_config = {
    resources = ["secrets"]
  }

  # Managed addons land in eks-addons.tf (kept separate for clearer Pod Identity wiring).
  addons = {}

  eks_managed_node_groups = {
    system = {
      instance_types = local.ng.system.instance_types
      capacity_type  = "ON_DEMAND"
      min_size       = local.ng.system.min_size
      desired_size   = local.ng.system.desired_size
      max_size       = local.ng.system.max_size
      labels         = { "node-role" = "system" }

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
      labels         = { "workload" = "prometheus", "node-role" = "stateful" }
      taints = {
        workload = {
          key    = "workload"
          value  = "prometheus"
          effect = "NO_SCHEDULE"
        }
      }
      metadata_options = {
        http_tokens                 = "required"
        http_put_response_hop_limit = 1
        http_endpoint               = "enabled"
      }
    }

    jenkins_system = {
      instance_types = local.ng.jenkins_system.instance_types
      capacity_type  = "ON_DEMAND"
      min_size       = local.ng.jenkins_system.min_size
      desired_size   = local.ng.jenkins_system.desired_size
      max_size       = local.ng.jenkins_system.max_size
      subnet_ids     = [module.vpc.private_subnets[0]] # us-east-1a — EBS zonality
      labels         = { "workload" = "jenkins", "node-role" = "stateful" }
      taints = {
        workload = {
          key    = "workload"
          value  = "jenkins"
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

  tags = local.tags
}
