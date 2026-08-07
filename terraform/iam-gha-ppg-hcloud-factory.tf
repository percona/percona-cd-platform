# Owner: postgresql
#
# GitHub-OIDC role + SSM token parameter for the PPG Hetzner snapshot factory
# (ppg-hcloud-factory / ppg-hcloud-janitor workflows in
# Percona-Lab/jenkins-pipelines). The workflows assume the master-pinned role
# and read the Hetzner token from SSM, so no Hetzner credential lives in
# GitHub and a branch-ref dispatch fails at AssumeRole. Trust shape is owned
# by ./modules/github-oidc-role.

# The real value is set out-of-band (aws ssm put-parameter --overwrite) so
# the token never enters git or the Terraform state.
resource "aws_ssm_parameter" "ppg_hcloud_factory_token" {
  name        = "/ppg/hcloud-factory-token"
  description = "Project-scoped Hetzner Cloud API token for the PPG snapshot factory (bake, smoke, promote, prune, janitor)."
  type        = "SecureString"
  value       = "REPLACE-out-of-band"

  tags = merge(local.tags, { team = "postgresql" })

  lifecycle {
    ignore_changes = [value]
  }
}

data "aws_iam_policy_document" "gha_ppg_hcloud_factory_perms" {
  statement {
    sid       = "ReadFactoryToken"
    effect    = "Allow"
    actions   = ["ssm:GetParameter"]
    resources = [aws_ssm_parameter.ppg_hcloud_factory_token.arn]
  }
}

module "ppg_hcloud_factory_oidc" {
  source = "./modules/github-oidc-role"

  name             = "gha-ppg-hcloud-factory"
  role_name_prefix = "${local.cluster_name}-"
  description      = "Assumed by the PPG Hetzner snapshot-factory and janitor GHA workflows in Percona-Lab/jenkins-pipelines (master) via OIDC to read the Hetzner API token from SSM. No static keys."

  subject_claims          = var.ppg_hcloud_factory_subject_claims
  permissions_policy_json = data.aws_iam_policy_document.gha_ppg_hcloud_factory_perms.json
  tags                    = merge(local.tags, { team = "postgresql" })
}

output "ppg_hcloud_factory_oidc_role_arn" {
  description = "role-to-assume for the PPG Hetzner-factory GHA workflows (optional repo secret PPG_HCLOUD_FACTORY_ROLE_ARN; the workflows fall back to the prod ARN)."
  value       = module.ppg_hcloud_factory_oidc.role_arn
}
