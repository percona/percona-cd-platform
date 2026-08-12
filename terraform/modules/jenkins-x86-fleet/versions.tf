# Aligned with the root + jenkins-master modules (OpenTofu >= 1.11, AWS ~> 6.43)
# so a caller can drop this module in without bumping anything. A single aws
# provider is used; the caller maps the regional alias via `providers = {...}`.
terraform {
  required_version = ">= 1.11.0, < 2.0.0"
  required_providers {
    aws = {
      source  = "hashicorp/aws"
      version = ">= 6.43.0, < 7.0.0"
    }
  }
}
