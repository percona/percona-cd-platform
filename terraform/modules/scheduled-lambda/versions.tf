# Provider/engine constraints. Aligned with the root module's
# terraform/versions.tf (OpenTofu >= 1.11, AWS provider ~> 6.43) so a caller
# can drop this module in without bumping anything. `archive` is required for
# the data.archive_file that packages the Lambda source directory into a zip;
# add the matching constraint + lock entry to the root module too.
terraform {
  required_version = ">= 1.11.0, < 2.0.0"
  required_providers {
    aws = {
      source  = "hashicorp/aws"
      version = ">= 6.43.0, < 7.0.0"
    }
    archive = {
      source  = "hashicorp/archive"
      version = ">= 2.4.0, < 3.0.0"
    }
  }
}
