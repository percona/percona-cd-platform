# Owner: platform
provider "aws" {
  region = var.aws_region
  # Empty string -> SDK default credential chain (env vars, SSO, instance profile, etc.).
  # Set var.aws_profile via local.auto.tfvars (gitignored) or AWS_PROFILE env var.
  profile = var.aws_profile != "" ? var.aws_profile : null

  default_tags {
    tags = local.tags
  }
}

# Aliased providers for the Jenkins master regions. Used by the master-<host>.tf peering blocks to
# create cross-region VPC peering accepters and ps3-side route table entries
# in each master's own region. Only the regions hosting a Jenkins master
# need an alias; us-east-1 is covered by the default provider above.
provider "aws" {
  alias   = "eu-west-1"
  region  = "eu-west-1"
  profile = var.aws_profile != "" ? var.aws_profile : null
  default_tags { tags = local.tags }
}

provider "aws" {
  alias   = "us-east-2"
  region  = "us-east-2"
  profile = var.aws_profile != "" ? var.aws_profile : null
  default_tags { tags = local.tags }
}

provider "aws" {
  alias   = "us-west-1"
  region  = "us-west-1"
  profile = var.aws_profile != "" ? var.aws_profile : null
  default_tags { tags = local.tags }
}

provider "aws" {
  alias   = "us-west-2"
  region  = "us-west-2"
  profile = var.aws_profile != "" ? var.aws_profile : null
  default_tags { tags = local.tags }
}

provider "aws" {
  alias   = "eu-central-1"
  region  = "eu-central-1"
  profile = var.aws_profile != "" ? var.aws_profile : null
  default_tags { tags = local.tags }
}

# Kubernetes + Helm providers configure once the EKS cluster exists.
# Until then they will fail-fast on apply, which is the intended order.
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
