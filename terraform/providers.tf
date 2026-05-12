provider "aws" {
  region = var.aws_region
  # Empty string -> SDK default credential chain (env vars, SSO, instance profile, etc.).
  # Set var.aws_profile via local.auto.tfvars (gitignored) or AWS_PROFILE env var.
  profile = var.aws_profile != "" ? var.aws_profile : null

  default_tags {
    tags = local.tags
  }
}

# Kubernetes + Helm + kubectl providers configure once the EKS cluster exists.
# Until then they will fail-fast on apply, which is the intended order.
#
# Single-state by design: this root owns both AWS foundation (VPC, EKS, IAM,
# Route 53, ACM, KMS) and the ArgoCD bootstrap (4 resources in argocd.tf).
# After ArgoCD becomes healthy, every cluster-side change is reconciled by
# ApplicationSets reading from `resources/` -- Terraform stops touching the
# cluster. See ADR 0005 ("GitOps Bridge bootstrap pattern") for the rationale.
# Splitting into separate states (one AWS-only, one cluster-bootstrap) would
# duplicate the handoff machinery for what is currently a 4-resource boundary;
# we accept the documented "provider config derived from module outputs" caveat
# in exchange for a single tofu apply that takes the cluster all the way to
# ArgoCD-healthy.
provider "kubernetes" {
  host                   = try(module.eks.cluster_endpoint, "")
  cluster_ca_certificate = try(base64decode(module.eks.cluster_certificate_authority_data), "")
  exec {
    api_version = "client.authentication.k8s.io/v1beta1"
    command     = "aws"
    args = concat(
      ["eks", "get-token", "--cluster-name", local.cluster_name, "--region", var.aws_region],
      var.aws_profile != "" ? ["--profile", var.aws_profile] : []
    )
  }
}

provider "helm" {
  kubernetes {
    host                   = try(module.eks.cluster_endpoint, "")
    cluster_ca_certificate = try(base64decode(module.eks.cluster_certificate_authority_data), "")
    exec {
      api_version = "client.authentication.k8s.io/v1beta1"
      command     = "aws"
      args = concat(
        ["eks", "get-token", "--cluster-name", local.cluster_name, "--region", var.aws_region],
        var.aws_profile != "" ? ["--profile", var.aws_profile] : []
      )
    }
  }
}

provider "kubectl" {
  host                   = try(module.eks.cluster_endpoint, "")
  cluster_ca_certificate = try(base64decode(module.eks.cluster_certificate_authority_data), "")
  load_config_file       = false
  exec {
    api_version = "client.authentication.k8s.io/v1beta1"
    command     = "aws"
    args = concat(
      ["eks", "get-token", "--cluster-name", local.cluster_name, "--region", var.aws_region],
      var.aws_profile != "" ? ["--profile", var.aws_profile] : []
    )
  }
}
