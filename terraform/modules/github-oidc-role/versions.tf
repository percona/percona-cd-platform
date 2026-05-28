# Provider/engine constraints. Aligned with the root module's
# `terraform/versions.tf` (OpenTofu >= 1.11, AWS provider ~> 6.43) so a
# caller can drop this module in without bumping anything.
terraform {
  required_version = ">= 1.11.0, < 2.0.0"
  required_providers {
    aws = {
      source  = "hashicorp/aws"
      version = ">= 6.43.0, < 7.0.0"
    }
  }
}
