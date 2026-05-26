# ps80.cd.percona.com -- Terraform-managed via ./modules/jenkins-master/.
# Migrated from CloudFormation stack jenkins-ps80
# (Percona-Lab/jenkins-pipelines/IaC/ps80.cd) on 2026-05-XX.
#
# Switched from SpotFleet to a single on-demand instance via the module's
# new purchasing_option toggle. Two spot reclamations in 24h on 2026-05-25
# and 2026-05-26 killed MTR MatrixBuild parents (#38, #39) and ~12 arm64
# child builds; on-demand at right-sized c7i-flex.large is +$17/mo over
# the prior c5d.xlarge spot blend and pays back on the first avoided
# reclamation.
#
# This file covers the TF + on-demand cut only. Phase 2 (SSL strip + EKS
# peering + drop EIP + external-dns ownership) mirrors PS-10945 (ps3) and
# lands in a follow-up.

module "ps80" {
  source    = "./modules/jenkins-master"
  providers = { aws = aws.us-west-2 }

  hostname                = "ps80.cd.percona.com"
  short_name              = "jenkins-ps80"
  vpc_cidr                = "10.155.0.0/22"
  ami_id                  = "ami-06a974f9b8a97ecf2" # Amazon Linux 2023 in us-west-2
  master_profile          = "eks_observability"
  jenkins_package_version = "2.541.3"
  cache_bucket_name       = "ps-build-cache"
  ebs_size                = 200

  purchasing_option       = "on-demand"
  on_demand_instance_type = "c7i-flex.large"

  # Phase 1 keeps the EIP + Route53 record so DNS does not flip during the
  # CFN-to-TF cutover. Phase 2 (SSL cutover) drops both in favour of the
  # shared jenkins-cd ALB + external-dns.
  create_eip            = true
  create_route53_record = true

  extra_master_managed_policies = [
    "arn:aws:iam::aws:policy/CloudWatchAgentServerPolicy",
  ]

  extra_master_inline_policies = [
    {
      name = "AlloyGatewayBearerRead"
      json = data.aws_iam_policy_document.ps80_alloy_bearer_read.json
    },
  ]

  ssh_key_engineers = [
    "anderson.nogueira",
    "alex.miroshnychenko",
    "andrew.siemen",
    "eduardo.casarero",
    "evgeniy.patlan",
    "santiago.ruiz",
    "surabhi.bhat",
    "talha.rizwan",
    "vadim.yalovets",
  ]
}

# Rendered in the root so the ARN uses this account's caller-identity, not
# the us-west-2 alias's. Secret lives in us-east-1 with the rest of
# alloy-gateway.
data "aws_iam_policy_document" "ps80_alloy_bearer_read" {
  statement {
    sid       = "AlloyGatewayBearerRead"
    effect    = "Allow"
    actions   = ["secretsmanager:GetSecretValue"]
    resources = ["arn:aws:secretsmanager:us-east-1:${data.aws_caller_identity.current.account_id}:secret:percona-ci-platform/alloy-gateway/bearer-*"]
  }
}
