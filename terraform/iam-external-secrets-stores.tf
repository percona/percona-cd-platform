# Owner: platform
# One IAM role per External Secrets consumer (docs/adr/0050). The operator role
# assumes it for the consumer's scoped ClusterSecretStore. The role reads only
# the consumer's keys. It trusts only a session that the consumer's own store
# tagged with an allowed namespace, or with no namespace for the store validation.
#
# `exclusive = true` denies the consumer's keys to the operator role, so the
# shared store stops serving them. Set it only after every ExternalSecret of the
# consumer uses its scoped store and shows SecretSynced.

locals {
  eso_store_consumers = {
    jenkins-mcp = {
      namespaces   = ["jenkins-mcp"]
      secret_names = ["jenkins-mcp/fleet"]
      exclusive    = false
    }
    mimir = {
      namespaces   = ["mimir"]
      secret_names = ["mimir/alertmanager-smtp"]
      exclusive    = false
    }
  }

  # Secrets Manager appends a 6-character suffix to every secret ARN.
  eso_store_secret_arns = {
    for consumer, cfg in local.eso_store_consumers : consumer => [
      for secret in cfg.secret_names :
      "arn:aws:secretsmanager:${var.aws_region}:${data.aws_caller_identity.current.account_id}:secret:${local.cluster_name}/${secret}-??????"
    ]
  }

  eso_exclusive_secret_arns = flatten([
    for consumer, cfg in local.eso_store_consumers :
    local.eso_store_secret_arns[consumer] if cfg.exclusive
  ])
}

data "aws_iam_policy_document" "eso_store_trust" {
  for_each = local.eso_store_consumers

  statement {
    sid     = "ExternalSecretsOperatorAssume"
    effect  = "Allow"
    actions = ["sts:AssumeRole", "sts:TagSession"]

    principals {
      type        = "AWS"
      identifiers = [module.pod_identity_external_secrets.iam_role_arn]
    }

    # The operator validates a ClusterSecretStore with an empty esoNamespace tag.
    # Every read for an ExternalSecret carries the namespace of that ExternalSecret.
    condition {
      test     = "StringEquals"
      variable = "aws:RequestTag/esoNamespace"
      values   = concat(each.value.namespaces, [""])
    }

    # Only the consumer's own ClusterSecretStore may use the role.
    condition {
      test     = "StringEquals"
      variable = "aws:RequestTag/esoStoreName"
      values   = ["aws-secrets-manager-${each.key}"]
    }

    condition {
      test     = "StringEquals"
      variable = "aws:RequestTag/esoStoreKind"
      values   = ["ClusterSecretStore"]
    }
  }
}

data "aws_iam_policy_document" "eso_store_read" {
  for_each = local.eso_store_consumers

  statement {
    sid = "ReadOwnSecretsOnly"
    actions = [
      "secretsmanager:GetSecretValue",
      "secretsmanager:DescribeSecret",
    ]
    resources = local.eso_store_secret_arns[each.key]
  }
}

resource "aws_iam_role" "eso_store" {
  for_each = local.eso_store_consumers

  # Fixed name: the chart builds the role ARN from the account ID annotation
  # and this name, so no ARN is committed.
  name                 = "${local.cluster_name}-eso-${each.key}"
  description          = "Assumed by the External Secrets Operator for the ${each.key} ClusterSecretStore. Reads only the ${each.key} keys under ${local.cluster_name}/."
  assume_role_policy   = data.aws_iam_policy_document.eso_store_trust[each.key].json
  max_session_duration = 3600
  tags                 = local.tags
}

resource "aws_iam_role_policy" "eso_store_read" {
  for_each = local.eso_store_consumers

  name   = "read-own-secrets"
  role   = aws_iam_role.eso_store[each.key].id
  policy = data.aws_iam_policy_document.eso_store_read[each.key].json
}

# Identity-side grant on the operator's role to assume the consumer roles.
data "aws_iam_policy_document" "eso_assume_store_roles" {
  statement {
    sid       = "AssumeScopedStoreRoles"
    actions   = ["sts:AssumeRole", "sts:TagSession"]
    resources = [for role in aws_iam_role.eso_store : role.arn]
  }
}

resource "aws_iam_policy" "eso_assume_store_roles" {
  name        = "${local.cluster_name}-eso-assume-store-roles"
  description = "Lets the External-Secrets-Operator role assume the per-consumer store roles of ${local.cluster_name}"
  policy      = data.aws_iam_policy_document.eso_assume_store_roles.json
  tags        = local.tags
}

output "eso_store_role_names" {
  description = "Per-consumer ESO role names, the roleName of each scopedStores entry in resources/addons/external-secrets/values.yaml."
  value       = { for consumer, role in aws_iam_role.eso_store : consumer => role.name }
}
