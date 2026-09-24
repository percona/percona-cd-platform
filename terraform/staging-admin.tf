# Owner: platform
# Local login for the staging copies (terraform/master-<inst>-staging.tf).
# GitHub OAuth stays bound to the production hostnames, so each copy gets one
# local admin, staging-admin, whose password lives only here in state and in
# SSM Parameter Store as a SecureString. The copy's
# init.groovy.d/staging-admin.groovy reads its own parameter on the instance at
# boot, and `just staging-password <inst>` prints it for sharing.
#
# TEMPORARY: delete with the staging copies.

locals {
  staging_copies = toset(["pmm", "psmdb", "pg", "rel", "cloud"])
}

resource "random_password" "staging_admin" {
  for_each = local.staging_copies

  length  = 32
  special = false # letters and digits only, no quoting in shell or Groovy
}

resource "aws_ssm_parameter" "staging_admin_password" {
  for_each = local.staging_copies

  name        = "/percona-ci-platform/jenkins-staging/${each.key}/admin-password"
  description = "staging-admin password of ${each.key}-staging.cd.percona.com"
  type        = "SecureString"
  value       = random_password.staging_admin[each.key].result
}
