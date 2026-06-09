# Owner: platform
# State backend: S3 with native locking (`use_lockfile = true` — OpenTofu
# 1.10+). The bucket holds both the state file and its sibling .tflock object
# (S3 conditional writes via If-None-Match header). Bucket pre-created — see
# docs/runbooks/bootstrap-state.md.
# Credentials come from the AWS SDK default chain — set AWS_PROFILE before
# `tofu init` (or supply -backend-config=backend.hcl with `profile = "..."`).

terraform {
  backend "s3" {
    bucket       = "terraform-state-storage-percona-ci-platform"
    key          = "terraform.tfstate"
    region       = "us-east-1"
    encrypt      = true
    use_lockfile = true
  }
}
