# Owner: mysql
# IAM role assumed by GitHub Actions workflows in `percona/percona-server`
# via OIDC federation, used as the fallback path when Hetzner CAX capacity
# is exhausted for the arm64 build matrix.
#
# Scope: this file owns the fallback role only. Parent effort: the Cirrus ->
# GHA migration, completed before the 2026-06-01 Cirrus shutdown deadline.
#
# Validation:
#   End-to-end PoC validated on a personal fork (iter 11, 2026-05-28). The
#   policy below is the post-iteration shape that survives `RunInstances`,
#   `CreateTags`, `TerminateInstances`, and `GetConsoleOutput` in a real
#   GHA run; the deltas vs. the first draft are documented inline.
#
# Architecture:
#   Workflow steps run `aws sts assume-role-with-web-identity` against this
#   role, then call `ec2:RunInstances` to launch a single `c7g.4xlarge`
#   (Graviton) Spot or On-Demand instance in eu-central-1, drive the build,
#   and terminate it.
#
# Explicit non-permission: no `iam:PassRole`, no `aws_iam_instance_profile`
# resource. The launched EC2 instance gets ZERO AWS credentials; build
# code running on it (including PR code) cannot reach the AWS API. The
# trust split between "the workflow has narrow EC2 perms" and "the runner
# has none" is load-bearing.
#
# Trust shape is owned by the `github-oidc-role` module
# (`terraform/modules/github-oidc-role/`). This file owns only the
# workload-specific permissions policy + the module instantiation. See the
# module's main.tf header comment for why StringEquals (not StringLike)
# and the GitHub 2026-04-23 subject claim format change.
#
# Blast radius if a workflow's OIDC token leaks before it expires (15 min
# default):
#   - Attacker can launch ONE `c7g.4xlarge` in eu-central-1 carrying the
#     mandatory cleanup tags (so the percona-dev-admin Lambdas will reap
#     it within 10 min if the workflow's own terminate step doesn't fire).
#   - Attacker can terminate ONLY instances that already carry both
#     `iit-billing-tag=percona-server-gha-fallback` and
#     `github_repository=percona/percona-server` -- i.e. other in-flight
#     fallback runners from this same workflow. Cannot touch unrelated
#     eu-central-1 EC2.
#   - Attacker CANNOT: switch region, switch instance type, attach an
#     instance profile, touch IAM, read or write S3 / Secrets Manager /
#     KMS, reach the EKS cluster, or terminate any instance outside the
#     percona-server-gha-fallback tag scope.

# Permissions: region-locked, instance-type-locked, mandatory-tags-enforced
# RunInstances + the narrow set of read-only Describes and the public SSM
# AMI lookup the workflow needs. Lives in this file (not in the module)
# because the policy shape is workload-specific.
data "aws_iam_policy_document" "gha_percona_server_ec2_fallback_perms" {
  # RunInstances is a multi-resource action: it implicitly creates instance,
  # network-interface, volume, etc., and consumes image, subnet, security-
  # group, key-pair. IAM evaluates the action's grants PER RESOURCE TYPE.
  # Conditions like `aws:RequestTag/...` and `ec2:InstanceType` only make
  # sense for the INSTANCE resource and IAM will fail-closed on the other
  # resources if the same conditioned statement is used at Resource `*`.
  # Verified empirically on the fork PoC, 2026-05-28: a single Resource: *
  # statement got denied with `not authorized to perform: ec2:RunInstances
  # on resource: arn:aws:ec2:...:network-interface/*`.
  #
  # Split into two statements:
  #   1. Instance resource: full conditions (region, type, mandatory tags,
  #      tag-key allowlist)
  #   2. Supporting resources: region-only condition (just to prevent the
  #      role from launching into other regions)
  statement {
    sid    = "RunInstancesInstanceResource"
    effect = "Allow"
    actions = [
      "ec2:RunInstances",
    ]
    resources = ["arn:aws:ec2:eu-central-1:${data.aws_caller_identity.current.account_id}:instance/*"]

    condition {
      test     = "StringEquals"
      variable = "aws:RequestedRegion"
      values   = ["eu-central-1"]
    }

    condition {
      test     = "StringEquals"
      variable = "ec2:InstanceType"
      values   = ["c7g.4xlarge"]
    }

    # Mandatory cleanup tags. Without these, the percona-dev-admin cleanup
    # Lambdas terminate the instance (and later delete its volume);
    # enforcing at the IAM layer is belt-and-suspenders -- the workflow
    # physically cannot launch an instance that would be Lambda-killed.
    condition {
      test     = "StringEquals"
      variable = "aws:RequestTag/iit-billing-tag"
      values   = ["percona-server-gha-fallback"]
    }

    condition {
      test     = "StringEquals"
      variable = "aws:RequestTag/PerconaKeep"
      values   = ["True"]
    }

    # Workload-identity tag. Used by the TerminateInstances and CreateTags
    # resource-level conditions below to bound those calls to this
    # workflow's own instances.
    condition {
      test     = "StringEquals"
      variable = "aws:RequestTag/github_repository"
      values   = ["percona/percona-server"]
    }

    # Workflow-identity tag. Pinned so the role cannot launch instances
    # claiming to belong to some other workflow in the same repo.
    condition {
      test     = "StringEquals"
      variable = "aws:RequestTag/github_workflow"
      values   = ["build"]
    }

    # Exact allowlist of tag keys the workflow sends. Note underscores --
    # `RunInstances --tag-specifications` uses underscores in the keys,
    # not hyphens, and `ForAllValues:StringEquals` is exact-match (an
    # extra or mistyped key fails the launch).
    condition {
      test     = "ForAllValues:StringEquals"
      variable = "aws:TagKeys"
      values = [
        "iit-billing-tag",
        "PerconaKeep",
        "Name",
        "github_run_id",
        "github_run_attempt",
        "github_workflow",
        "github_repository",
      ]
    }
  }

  # Statement 2 of RunInstances split (see commentary above):
  # the supporting AWS-side resources implicitly used by RunInstances.
  # Region-only condition; no instance-type or tag conditions because they
  # do not apply to these resource types.
  statement {
    sid    = "RunInstancesSupportingResources"
    effect = "Allow"
    actions = [
      "ec2:RunInstances",
    ]
    resources = [
      "arn:aws:ec2:eu-central-1::image/*",
      "arn:aws:ec2:eu-central-1::snapshot/*",
      "arn:aws:ec2:eu-central-1:${data.aws_caller_identity.current.account_id}:network-interface/*",
      "arn:aws:ec2:eu-central-1:${data.aws_caller_identity.current.account_id}:subnet/*",
      "arn:aws:ec2:eu-central-1:${data.aws_caller_identity.current.account_id}:security-group/*",
      "arn:aws:ec2:eu-central-1:${data.aws_caller_identity.current.account_id}:volume/*",
      "arn:aws:ec2:eu-central-1:${data.aws_caller_identity.current.account_id}:key-pair/*",
    ]

    condition {
      test     = "StringEquals"
      variable = "aws:RequestedRegion"
      values   = ["eu-central-1"]
    }
  }

  # CreateTags. Required at RunInstances time when --tag-specifications is
  # passed. Conditions use aws:RequestTag (NOT aws:ResourceTag) because
  # the instance being tagged does not yet have any tags at create time;
  # the tags are part of THIS request. Using aws:ResourceTag here would
  # always evaluate to no-match and IAM would deny every launch (verified
  # empirically on the fork PoC, 2026-05-28). Bound to the eu-central-1
  # instance ARN namespace so the role cannot CreateTags on unrelated
  # instances.
  statement {
    sid       = "CreateTagsBoundedAndTagged"
    effect    = "Allow"
    actions   = ["ec2:CreateTags"]
    resources = ["arn:aws:ec2:eu-central-1:${data.aws_caller_identity.current.account_id}:instance/*"]

    condition {
      test     = "StringEquals"
      variable = "aws:RequestedRegion"
      values   = ["eu-central-1"]
    }

    condition {
      test     = "StringEquals"
      variable = "aws:RequestTag/iit-billing-tag"
      values   = ["percona-server-gha-fallback"]
    }

    condition {
      test     = "StringEquals"
      variable = "aws:RequestTag/github_repository"
      values   = ["percona/percona-server"]
    }

    condition {
      test     = "ForAllValues:StringEquals"
      variable = "aws:TagKeys"
      values = [
        "iit-billing-tag",
        "PerconaKeep",
        "Name",
        "github_run_id",
        "github_run_attempt",
        "github_workflow",
        "github_repository",
      ]
    }
  }

  # TerminateInstances. Bound to eu-central-1 instance ARNs whose existing
  # tags identify them as a percona-server fallback runner. Combined with
  # `instance-initiated-shutdown-behavior=terminate` set at RunInstances
  # time, this is the orphan-reaper safety net; even if creds leak, the
  # role cannot terminate unrelated eu-central-1 EC2 instances.
  statement {
    sid       = "TerminateOwnFallbackInstances"
    effect    = "Allow"
    actions   = ["ec2:TerminateInstances"]
    resources = ["arn:aws:ec2:eu-central-1:${data.aws_caller_identity.current.account_id}:instance/*"]

    condition {
      test     = "StringEquals"
      variable = "aws:RequestedRegion"
      values   = ["eu-central-1"]
    }

    condition {
      test     = "StringEquals"
      variable = "aws:ResourceTag/iit-billing-tag"
      values   = ["percona-server-gha-fallback"]
    }

    condition {
      test     = "StringEquals"
      variable = "aws:ResourceTag/github_repository"
      values   = ["percona/percona-server"]
    }
  }

  # Read-only Describes for AZ / subnet / SG / spot probing and AMI
  # resolution. None of these support resource-level permissions; region
  # scoping is the only available knob.
  statement {
    sid    = "DescribeReadOnlyInRegion"
    effect = "Allow"
    actions = [
      "ec2:DescribeInstances",
      "ec2:DescribeImages",
      "ec2:DescribeInstanceTypes",
      "ec2:DescribeAvailabilityZones",
      "ec2:DescribeSubnets",
      "ec2:DescribeVpcs",
      "ec2:DescribeSecurityGroups",
      "ec2:DescribeSpotInstanceRequests",
    ]
    resources = ["*"]

    condition {
      test     = "StringEquals"
      variable = "aws:RequestedRegion"
      values   = ["eu-central-1"]
    }
  }

  # Canonical's public Ubuntu arm64 AMI ID SSM parameter. The version
  # segment is wildcarded so a runner OS bump (24.04 -> 26.04 and beyond)
  # needs no IAM change, and branches on different versions (8.0 on 24.04,
  # 8.4/9.7 on 26.04) both resolve. Still bound to the canonical arm64
  # gp3 path; Canonical owns the parameter, the workflow only reads it.
  statement {
    sid     = "ReadCanonicalUbuntuArm64Ami"
    effect  = "Allow"
    actions = ["ssm:GetParameter"]
    resources = [
      "arn:aws:ssm:eu-central-1::parameter/aws/service/canonical/ubuntu/server/*/stable/current/arm64/hvm/ebs-gp3/ami-id",
    ]
  }

  # GetConsoleOutput for post-mortem debugging when the runner fails to
  # register (the no-IAM-on-VM design rules out SSM-ing in, and self-
  # termination removes local logs). Read-only; bounded to the
  # eu-central-1 instance ARN namespace. Called by the workflow's "Wait
  # for runner to come online" step on timeout.
  statement {
    sid       = "GetConsoleOutputForDebug"
    effect    = "Allow"
    actions   = ["ec2:GetConsoleOutput"]
    resources = ["arn:aws:ec2:eu-central-1:${data.aws_caller_identity.current.account_id}:instance/*"]

    condition {
      test     = "StringEquals"
      variable = "aws:RequestedRegion"
      values   = ["eu-central-1"]
    }
  }
}

# Trust + role wiring delegated to the in-repo module. The four-entry
# subject_claims list intentionally pre-includes `refs/heads/trunk` and
# `refs/heads/8.4` so the planned trunk + 8.4 follow-ups do not require an
# IAM edit when they land.
module "gha_percona_server_ec2_fallback" {
  source = "./modules/github-oidc-role"

  name             = "gha-percona-server-ec2-fallback"
  role_name_prefix = "${local.cluster_name}-"
  description      = "Assumed by GitHub Actions workflows in percona/percona-server via OIDC to launch the c7g.4xlarge fallback runner when Hetzner CAX capacity is exhausted (PS-11219, parent PS-11078)."

  subject_claims = [
    "repo:percona/percona-server:ref:refs/heads/8.0",
    "repo:percona/percona-server:ref:refs/heads/trunk",
    "repo:percona/percona-server:ref:refs/heads/8.4",
    "repo:percona/percona-server:pull_request",
  ]

  permissions_policy_json = data.aws_iam_policy_document.gha_percona_server_ec2_fallback_perms.json

  tags = merge(local.tags, { team = "mysql" })
}

# Surface the role ARN so the operator can plug it into the
# `percona/percona-server` workflow's `aws-actions/configure-aws-credentials`
# step. ARN is not a secret (any account-resident principal can read IAM
# role ARNs); exposing as a plain output is fine and avoids hand-copying
# from the AWS console.
output "gha_percona_server_ec2_fallback_role_arn" {
  description = "Role ARN to set as `role-to-assume` in percona/percona-server GHA workflow (Hetzner-exhausted fallback)."
  value       = module.gha_percona_server_ec2_fallback.role_arn
}
