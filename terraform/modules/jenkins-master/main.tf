# Owner: platform
# jenkins-master module. Maps a per-master CF stack (jenkins-pipelines/IaC/
# <host>.cd/JenkinsStack.yml) to Terraform; consumed once per master from
# terraform/master-<host>.tf with `providers = { aws = aws.<region> }`.
# Built against the original ps3 cutover as the canonical shape; non-ps3 quirks are
# carried via variables.tf toggles.

locals {
  # iit-billing-tag = short_name overrides provider default_tags so cleanup
  # Lambdas don't terminate the master (see docs/runbooks/cleanup-reapers.md).
  # Module-set keys are merged LAST so a caller-supplied map can never clobber
  # the per-instance billing/team identity.
  base_tags = merge(
    var.tags,
    {
      Name              = var.short_name
      "iit-billing-tag" = var.short_name
      team              = var.team
    },
  )

  worker_logical = var.worker_role_legacy_naming ? "slave" : "worker"
  worker_name    = "${var.short_name}-${local.worker_logical}"

  # CF used AZ index 1 (B) + 2 (C); extra_subnet_a adds 0 (A) for psmdb.
  subnet_indices = merge(
    { "b" = 1, "c" = 2 },
    var.extra_subnet_a ? { "a" = 0 } : {},
  )

  master_subnet_key = [for k, v in local.subnet_indices : k if v == var.az_index][0]
}

data "aws_availability_zones" "available" {
  state = "available"
}

resource "aws_vpc" "this" {
  cidr_block           = var.vpc_cidr
  enable_dns_support   = true
  enable_dns_hostnames = true
  instance_tenancy     = "default"
  tags                 = local.base_tags

  lifecycle {
    prevent_destroy = true
  }
}

resource "aws_internet_gateway" "this" {
  vpc_id = aws_vpc.this.id
  tags   = local.base_tags
}

resource "aws_subnet" "this" {
  for_each = local.subnet_indices

  vpc_id                  = aws_vpc.this.id
  cidr_block              = cidrsubnet(var.vpc_cidr, 2, each.value) # /24 inside /22
  availability_zone       = data.aws_availability_zones.available.names[each.value]
  map_public_ip_on_launch = true

  tags = merge(local.base_tags, {
    Name = "${var.short_name}-${upper(each.key)}"
  })
}

resource "aws_route_table" "this" {
  vpc_id = aws_vpc.this.id
  tags   = local.base_tags
}

# Default-route to IGW. Explicit dependency mirrors CF's DependsOn
# (prevents the route/IGW race on UPDATE-replace).
resource "aws_route" "internet" {
  route_table_id         = aws_route_table.this.id
  destination_cidr_block = "0.0.0.0/0"
  gateway_id             = aws_internet_gateway.this.id

  depends_on = [aws_internet_gateway.this]
}

resource "aws_route_table_association" "this" {
  for_each       = local.subnet_indices
  subnet_id      = aws_subnet.this[each.key].id
  route_table_id = aws_route_table.this.id
}

resource "aws_vpc_endpoint" "s3" {
  vpc_id            = aws_vpc.this.id
  service_name      = "com.amazonaws.${data.aws_region.current.region}.s3"
  vpc_endpoint_type = "Gateway"
  route_table_ids   = [aws_route_table.this.id]
  policy            = data.aws_iam_policy_document.s3_endpoint.json
  tags              = local.base_tags
}

data "aws_region" "current" {}

# Gateway endpoint policy is a defense-in-depth FILTER, not the access-control
# layer: a Gateway endpoint intercepts ALL same-region S3 traffic from the VPC,
# so any action it omits is denied fleet-wide regardless of IAM. IAM (the master
# role, the worker roles, percona-openshift-user) is the real control. An
# action-scoped list silently 403s workloads needing other S3 ops (e.g.
# openshift-install destroy emptying a versioned bucket needs
# s3:ListBucketVersions). Allow all S3 actions and let IAM scope per principal.
# Resource stays "*" on purpose: workers legitimately read cross-account public
# buckets (AL2023 package mirrors, OpenShift/Red Hat release images), which an
# aws:ResourceOrgID/ResourceAccount restriction would break. See docs/adr/0033.
data "aws_iam_policy_document" "s3_endpoint" {
  statement {
    effect = "Allow"
    principals {
      type        = "*"
      identifiers = ["*"]
    }
    actions   = ["s3:*"]
    resources = ["*"]
  }
}

# init.groovy.d delivery bucket (opt-in via init_groovy_files and/or
# init_groovy_template_files). One private, versioned bucket per master,
# co-located in the master's region so the boot-time `aws s3 cp` transits the
# existing S3 gateway VPC endpoint (aws_vpc_endpoint.s3) rather than NAT
# egress. Terraform uploads the objects with the operator's creds; the master
# role only gets read (see the InitGroovyConfig statement on
# data.aws_iam_policy_document.master below).
#
# Template entries render with this module's own network identities, so a
# subnet or VPC replacement re-renders the uploaded object in the same apply
# instead of relying on a hand-edited literal catching up later (the
# "re-apply re-stales netMap" failure class). Rendered entries win over raw
# init_groovy_files on filename collision.
locals {
  init_groovy_rendered = {
    for name, path in var.init_groovy_template_files :
    name => templatefile(path, {
      subnet_by_az_name = { for k, s in aws_subnet.this : s.availability_zone => s.id }
      vpc_id            = aws_vpc.this.id
    })
  }
  init_groovy_all = merge(var.init_groovy_files, local.init_groovy_rendered)
}

resource "aws_s3_bucket" "init_config" {
  count  = length(local.init_groovy_all) > 0 ? 1 : 0
  bucket = "${var.short_name}-init-config"

  tags = local.base_tags
}

resource "aws_s3_bucket_versioning" "init_config" {
  count  = length(local.init_groovy_all) > 0 ? 1 : 0
  bucket = aws_s3_bucket.init_config[0].id
  versioning_configuration {
    status = "Enabled"
  }
}

resource "aws_s3_bucket_public_access_block" "init_config" {
  count                   = length(local.init_groovy_all) > 0 ? 1 : 0
  bucket                  = aws_s3_bucket.init_config[0].id
  block_public_acls       = true
  block_public_policy     = true
  ignore_public_acls      = true
  restrict_public_buckets = true
}

# etag = md5(content) so a content change re-uploads the object. Key mirrors
# the on-disk layout the boot path writes to (init.groovy.d/<filename>).
resource "aws_s3_object" "init_config" {
  for_each = local.init_groovy_all

  bucket  = aws_s3_bucket.init_config[0].id
  key     = "init.groovy.d/${each.key}"
  content = each.value
  etag    = md5(each.value)

  tags = local.base_tags
}

# Post-boot drift gate for init.groovy.d. The user_data fetch runs once per
# instance lifetime, so an S3 content change after boot (e.g. a re-rendered
# netMap) otherwise sits undelivered until instance replacement and a JVM
# restart silently loads the stale EBS copy (the 2026-06-10 cloud.cd
# incident). The association re-syncs on a schedule; runs once immediately
# on creation. Additive sync (no --delete): bucket-managed files converge,
# files that exist only on disk survive.
# On-demand masters only: the association targets the stable aws_instance id
# (the spot path has no aws_instance and replaces instances on reclaim).
resource "aws_ssm_association" "init_groovy_sync" {
  count = var.init_groovy_sync_schedule != null && length(local.init_groovy_all) > 0 && var.purchasing_option == "on-demand" ? 1 : 0

  name                = "AWS-RunShellScript"
  association_name    = "${var.short_name}-init-groovy-sync"
  schedule_expression = var.init_groovy_sync_schedule

  targets {
    key    = "InstanceIds"
    values = [aws_instance.master[0].id]
  }

  parameters = {
    commands = join("\n", [
      "set -eu",
      "aws s3 sync s3://${aws_s3_bucket.init_config[0].id}/init.groovy.d/ /mnt/${var.hostname}/init.groovy.d/",
      "chown -R jenkins:jenkins /mnt/${var.hostname}/init.groovy.d",
    ])
  }
}

resource "aws_security_group" "ssh" {
  name        = "SSH"
  description = "SSH traffic in"
  vpc_id      = aws_vpc.this.id

  ingress {
    from_port   = 22
    to_port     = 22
    protocol    = "tcp"
    cidr_blocks = var.ssh_allowed_cidrs
  }

  egress {
    from_port   = 0
    to_port     = 0
    protocol    = "-1"
    cidr_blocks = ["0.0.0.0/0"]
  }

  tags = local.base_tags
}

resource "aws_security_group" "http" {
  name        = "HTTP"
  description = "HTTP and HTTPS traffic in"
  vpc_id      = aws_vpc.this.id

  # Ingress is solely var.extra_http_ingress: :8080 from the EKS VPC CIDR
  # over the per-master peering for the jenkins-ingress NGINX, plus rare
  # per-master extras (pxc :50000 inbound JNLP). The CFN-era world-open
  # :80/:443 was removed once every consumer of this module moved behind
  # the ALB: TLS terminates at the ALB and nothing on the master listens
  # on those ports (user-data installs no openresty or certbot). `name` and
  # `description` are frozen, changing either forces SG replacement.
  # See docs/connectivity.md "Security groups".
  dynamic "ingress" {
    for_each = var.extra_http_ingress
    content {
      from_port   = ingress.value.port
      to_port     = ingress.value.port
      protocol    = "tcp"
      cidr_blocks = [ingress.value.cidr]
    }
  }

  egress {
    from_port   = 0
    to_port     = 0
    protocol    = "-1"
    cidr_blocks = ["0.0.0.0/0"]
  }

  tags = local.base_tags
}

data "aws_iam_policy_document" "ec2_assume" {
  statement {
    effect  = "Allow"
    actions = ["sts:AssumeRole"]
    principals {
      type        = "Service"
      identifiers = ["ec2.amazonaws.com"]
    }
  }
}

data "aws_iam_policy_document" "spot_fleet_assume" {
  statement {
    effect  = "Allow"
    actions = ["sts:AssumeRole"]
    principals {
      type        = "Service"
      identifiers = ["spotfleet.amazonaws.com"]
    }
  }
}

resource "aws_iam_role" "worker" {
  name               = local.worker_name
  path               = "/"
  assume_role_policy = data.aws_iam_policy_document.ec2_assume.json
  tags               = local.base_tags
}

# EC2 tag perms always; conditional extras per caller: S3 cache
# (cache_bucket_name), Packer amazon-ebs lifecycle (worker_ami_builder),
# ECR read + public-ECR auth (worker_ecr_read).
data "aws_iam_policy_document" "worker" {
  statement {
    sid    = "EC2Tags"
    effect = "Allow"
    actions = [
      "ec2:CreateTags",
      "ec2:DeleteTags",
      "ec2:DescribeInstances",
      "ec2:DescribeSpotInstanceRequests",
    ]
    resources = ["*"]
  }

  dynamic "statement" {
    for_each = var.cache_bucket_name == null ? [] : [var.cache_bucket_name]
    content {
      sid    = "CacheBucket"
      effect = "Allow"
      actions = [
        "s3:ListBucket",
        "s3:GetObject",
        "s3:GetObjectAcl",
        "s3:PutObject",
        "s3:PutObjectAcl",
        "s3:DeleteObject",
        "s3:AbortMultipartUpload",
      ]
      resources = [
        "arn:aws:s3:::${statement.value}",
        "arn:aws:s3:::${statement.value}/*",
      ]
    }
  }

  # Packer amazon-ebs lifecycle (worker_ami_builder): the builder launches a
  # temp instance with a temp keypair, snapshots it into an AMI, and tears
  # everything down, all under the worker's instance-profile credentials.
  # DescribeRegions is Packer's first call (region validation), so a missing
  # grant fails the build before anything launches. Tag and DescribeInstances
  # actions ride the base EC2Tags statement.
  dynamic "statement" {
    for_each = var.worker_ami_builder ? [1] : []
    content {
      sid    = "AmiBuilder"
      effect = "Allow"
      actions = [
        "ec2:AttachVolume",
        "ec2:CancelSpotInstanceRequests",
        "ec2:CopyImage",
        "ec2:CreateImage",
        "ec2:CreateKeyPair",
        "ec2:CreateSnapshot",
        "ec2:CreateVolume",
        "ec2:DeleteKeyPair",
        "ec2:DeleteSnapshot",
        "ec2:DeleteVolume",
        "ec2:DeregisterImage",
        "ec2:DescribeImages",
        "ec2:DescribeInstanceStatus",
        "ec2:DescribeRegions",
        "ec2:DescribeSecurityGroups",
        "ec2:DescribeSnapshots",
        "ec2:DescribeSubnets",
        "ec2:DescribeVolumes",
        "ec2:DetachVolume",
        "ec2:ModifyImageAttribute",
        "ec2:ModifyInstanceAttribute",
        "ec2:RegisterImage",
        "ec2:RequestSpotInstances",
        "ec2:RunInstances",
        "ec2:StopInstances",
        "ec2:TerminateInstances",
      ]
      resources = ["*"]
    }
  }

  # Private-ECR read + public-ECR auth (worker_ecr_read).
  # sts:GetServiceBearerToken is required alongside
  # ecr-public:GetAuthorizationToken for authenticated public-gallery pulls.
  dynamic "statement" {
    for_each = var.worker_ecr_read ? [1] : []
    content {
      sid    = "EcrRead"
      effect = "Allow"
      actions = [
        "ecr:BatchCheckLayerAvailability",
        "ecr:BatchGetImage",
        "ecr:DescribeImages",
        "ecr:DescribeRepositories",
        "ecr:GetAuthorizationToken",
        "ecr:GetDownloadUrlForLayer",
        "ecr-public:GetAuthorizationToken",
        "sts:GetServiceBearerToken",
      ]
      resources = ["*"]
    }
  }
}

resource "aws_iam_role_policy" "worker" {
  name   = local.worker_name
  role   = aws_iam_role.worker.id
  policy = data.aws_iam_policy_document.worker.json
}

resource "aws_iam_role_policy_attachment" "worker_extra" {
  for_each   = toset(var.extra_worker_managed_policies)
  role       = aws_iam_role.worker.name
  policy_arn = each.value
}

resource "aws_iam_instance_profile" "worker" {
  name = local.worker_name
  path = "/"
  role = aws_iam_role.worker.name
  tags = local.base_tags
}

# Master role: 4 inline policies in CF order (StartInstances, PassRole,
# UserDataNeeds, ReadTerminationSQS) + caller extras. SSM Managed Instance
# Core is attached as baseline so every master is reachable via `paws ssh
# --ssm` without an EIP.
resource "aws_iam_role" "master" {
  name               = "${var.short_name}-master"
  path               = "/"
  assume_role_policy = data.aws_iam_policy_document.ec2_assume.json
  tags               = local.base_tags
}

resource "aws_iam_role_policy_attachment" "master_managed" {
  for_each = toset(concat(
    ["arn:aws:iam::aws:policy/AmazonSSMManagedInstanceCore"],
    var.extra_master_managed_policies,
  ))
  role       = aws_iam_role.master.name
  policy_arn = each.value
}

data "aws_iam_policy_document" "master_start_instances" {
  statement {
    effect = "Allow"
    actions = [
      "ec2:ModifySpotFleetRequest",
      "ec2:DescribeSpotFleetRequests",
      "ec2:DescribeSpotFleetInstances",
      "ec2:DescribeSpotInstanceRequests",
      "ec2:CancelSpotInstanceRequests",
      "ec2:GetConsoleOutput",
      "ec2:RequestSpotInstances",
      "ec2:RunInstances",
      "ec2:StartInstances",
      "ec2:StopInstances",
      "ec2:TerminateInstances",
      "ec2:CreateTags",
      "ec2:DeleteTags",
      "ec2:DescribeInstances",
      "ec2:DescribeKeyPairs",
      "ec2:DescribeRegions",
      "ec2:DescribeImages",
      "ec2:DescribeAvailabilityZones",
      "ec2:DescribeSecurityGroups",
      "ec2:DescribeSubnets",
      # Read by every master's spot-price-auto-updater job to refresh
      # template spot bid ceilings.
      "ec2:DescribeSpotPriceHistory",
    ]
    resources = ["*"]
  }
}

data "aws_iam_policy_document" "master_pass_role" {
  statement {
    effect = "Allow"
    actions = [
      "iam:ListRoles",
      "iam:PassRole",
      "iam:ListInstanceProfiles",
      "iam:ListInstanceProfilesForRole",
    ]
    resources = [
      aws_iam_role.worker.arn,
      aws_iam_instance_profile.worker.arn,
    ]
  }
}

data "aws_iam_policy_document" "master_user_data_needs" {
  statement {
    effect = "Allow"
    actions = [
      "ec2:AttachVolume",
      "ec2:DetachVolume",
      "ec2:DescribeVolumes",
      "ec2:AssociateAddress",
      "ec2:CreateTags",
      "ec2:DescribeInstances",
    ]
    resources = ["*"]
  }
}

data "aws_iam_policy_document" "master_read_termination_sqs" {
  count = var.create_termination_queue ? 1 : 0
  statement {
    effect = "Allow"
    actions = [
      "sqs:GetQueueUrl",
      "sqs:ReceiveMessage",
      "sqs:DeleteMessage",
      "sqs:DeleteMessageBatch",
    ]
    resources = [aws_sqs_queue.termination[0].arn]
  }
}

# Read-only access to the init.groovy.d delivery bucket. Only the boot-time
# `aws s3 cp` consumes this; the upload is done by Terraform, not the master.
data "aws_iam_policy_document" "master_init_config" {
  count = length(local.init_groovy_all) > 0 ? 1 : 0
  statement {
    sid    = "InitGroovyConfig"
    effect = "Allow"
    actions = [
      "s3:GetObject",
      "s3:ListBucket",
    ]
    resources = [
      aws_s3_bucket.init_config[0].arn,
      "${aws_s3_bucket.init_config[0].arn}/*",
    ]
  }
}

resource "aws_iam_role_policy" "master_start_instances" {
  name   = "StartInstances"
  role   = aws_iam_role.master.id
  policy = data.aws_iam_policy_document.master_start_instances.json
}

resource "aws_iam_role_policy" "master_pass_role" {
  name   = "PassRole"
  role   = aws_iam_role.master.id
  policy = data.aws_iam_policy_document.master_pass_role.json
}

resource "aws_iam_role_policy" "master_user_data_needs" {
  name   = "UserDataNeeds"
  role   = aws_iam_role.master.id
  policy = data.aws_iam_policy_document.master_user_data_needs.json
}

resource "aws_iam_role_policy" "master_read_termination_sqs" {
  count  = var.create_termination_queue ? 1 : 0
  name   = "ReadTerminationSQS"
  role   = aws_iam_role.master.id
  policy = data.aws_iam_policy_document.master_read_termination_sqs[0].json
}

resource "aws_iam_role_policy" "master_init_config" {
  count  = length(local.init_groovy_all) > 0 ? 1 : 0
  name   = "InitGroovyConfig"
  role   = aws_iam_role.master.id
  policy = data.aws_iam_policy_document.master_init_config[0].json
}

resource "aws_iam_role_policy" "master_extras" {
  for_each = { for p in var.extra_master_inline_policies : p.name => p.json }

  name   = each.key
  role   = aws_iam_role.master.id
  policy = each.value
}

resource "aws_iam_instance_profile" "master" {
  # CF named the profile var.short_name (no -master suffix); preserve verbatim.
  name = var.short_name
  path = "/"
  role = aws_iam_role.master.name
  tags = local.base_tags
}

resource "aws_iam_role" "spot_fleet" {
  count              = var.purchasing_option == "spot" ? 1 : 0
  name               = "${var.short_name}-SpotFleet"
  assume_role_policy = data.aws_iam_policy_document.spot_fleet_assume.json
  tags               = local.base_tags
}

resource "aws_iam_role_policy_attachment" "spot_fleet_tagging" {
  count      = var.purchasing_option == "spot" ? 1 : 0
  role       = aws_iam_role.spot_fleet[0].name
  policy_arn = "arn:aws:iam::aws:policy/service-role/AmazonEC2SpotFleetTaggingRole"
}

# Vestigial. The CFN-era pmm stack defined a JWorkerUser IAM user, but it was
# deleted at cutover with zero access keys (nothing consumed it; pmm's jobs
# run on instance-profile credentials, with the AMI-build and ECR grants
# restored on the worker ROLE via worker_ami_builder / worker_ecr_read). The
# toggle stays fail-closed so no master recreates a long-lived-key user.
resource "aws_iam_user" "worker" {
  count = var.create_worker_user ? 1 : 0
  name  = "${var.short_name}-worker-user"
  path  = "/"
  tags  = local.base_tags
  lifecycle {
    precondition {
      condition     = !var.create_worker_user
      error_message = "create_worker_user=true is intentionally unimplemented; grant job-level permissions on the worker role (worker_ami_builder / worker_ecr_read / extra_worker_managed_policies) instead of an IAM user."
    }
  }
}

# No prevent_destroy: flipping create_eip=false deliberately releases the
# address (the masters are reached via the ALB for HTTPS and SSM for shell;
# the subnet auto-assigns a dynamic public IPv4 for outbound). Disassociate
# the address from the running instance BEFORE the destroying apply: user-data
# associates it at boot, and releasing an associated address fails.
resource "aws_eip" "master" {
  count  = var.create_eip ? 1 : 0
  domain = "vpc"
  tags   = local.base_tags

  depends_on = [aws_vpc.this]
}

resource "aws_ebs_volume" "data" {
  availability_zone = data.aws_availability_zones.available.names[var.az_index]
  size              = var.ebs_size
  type              = var.ebs_type
  # New masters create an encrypted volume from the start. Existing
  # volumes are NOT re-encrypted: lifecycle.ignore_changes covers
  # `encrypted` and `kms_key_id` so the AWS provider does not force
  # replace the live ps3 data volume. Encrypting the existing volume
  # is a separate ops procedure: snapshot -> CopySnapshot with KMS ->
  # create encrypted volume from copy -> detach old -> attach new.
  encrypted = true

  tags = merge(local.base_tags, {
    Name = "${var.short_name} DATA, do not remove"
    # Selects the volume into the per-region DLM snapshot policy
    # (terraform/ebs-snapshots.tf, docs/adr/0037-master-ebs-snapshot-rpo.md).
    "snapshot-policy" = "jenkins-master"
  })

  lifecycle {
    # prevent_destroy is the load-bearing safety; final_snapshot is omitted
    # because the AWS provider issues an empty ModifyVolume otherwise.
    prevent_destroy = true
    ignore_changes  = [snapshot_id, iops, throughput, encrypted, kms_key_id]
  }
}

# Spot-interruption SQS + EventBridge rule. psmdb opts out via the toggle.
resource "aws_sqs_queue" "termination" {
  count = var.create_termination_queue ? 1 : 0
  name  = "${var.short_name}-termination"
  # Encrypt with the SQS-managed key. The queue carries spot-lifecycle
  # event metadata (instance IDs, interrupt timestamps), not credentials,
  # but encrypting is a one-line hardening with no operational cost.
  sqs_managed_sse_enabled = true
  tags                    = local.base_tags
}

data "aws_iam_policy_document" "termination_queue" {
  count = var.create_termination_queue ? 1 : 0

  policy_id = "sqspolicy"

  statement {
    sid    = "First"
    effect = "Allow"
    principals {
      type        = "Service"
      identifiers = ["events.amazonaws.com"]
    }
    actions   = ["sqs:SendMessage"]
    resources = [aws_sqs_queue.termination[0].arn]
    condition {
      test     = "ArnEquals"
      variable = "aws:SourceArn"
      values   = [aws_cloudwatch_event_rule.termination[0].arn]
    }
  }
}

resource "aws_sqs_queue_policy" "termination" {
  count     = var.create_termination_queue ? 1 : 0
  queue_url = aws_sqs_queue.termination[0].url
  policy    = data.aws_iam_policy_document.termination_queue[0].json
}

resource "aws_cloudwatch_event_rule" "termination" {
  count       = var.create_termination_queue ? 1 : 0
  description = "EC2 Spot Instance Interruption Warning"
  state       = "ENABLED"
  event_pattern = jsonencode({
    source        = ["aws.ec2"]
    "detail-type" = ["EC2 Spot Instance Interruption Warning"]
  })
  tags = local.base_tags
}

resource "aws_cloudwatch_event_target" "termination" {
  count     = var.create_termination_queue ? 1 : 0
  rule      = aws_cloudwatch_event_rule.termination[0].name
  target_id = aws_sqs_queue.termination[0].name
  arn       = aws_sqs_queue.termination[0].arn
}

locals {
  user_data_rendered = templatefile("${path.module}/user-data.sh.tftpl", {
    jenkins_host            = var.hostname
    jenkins_short           = var.short_name
    eip_allocation_id       = var.create_eip ? aws_eip.master[0].id : ""
    data_volume_id          = aws_ebs_volume.data.id
    jenkins_package_version = var.jenkins_package_version
    packages                = join(" ", var.base_packages)
    master_profile          = var.master_profile
    ssh_key_engineers       = join(" ", var.ssh_key_engineers)
    plugin_install_hook     = var.plugin_install_hook == null ? "" : var.plugin_install_hook
    init_groovy_hooks       = var.init_groovy_hooks
    init_groovy_s3_bucket   = length(local.init_groovy_all) > 0 ? aws_s3_bucket.init_config[0].id : ""
    init_groovy_files       = keys(local.init_groovy_all)
    jvm_memory_opts         = var.jvm_memory_opts
  })
}

resource "aws_launch_template" "master" {
  name = var.launch_template_name != null ? var.launch_template_name : "${upper(replace(var.short_name, "jenkins-", ""))}MasterTemplate"

  image_id      = var.ami_id
  key_name      = var.master_key_name
  ebs_optimized = true

  iam_instance_profile {
    arn = aws_iam_instance_profile.master.arn
  }

  network_interfaces {
    device_index                = 0
    subnet_id                   = aws_subnet.this[local.master_subnet_key].id
    associate_public_ip_address = null
    security_groups = [
      aws_vpc.this.default_security_group_id,
      aws_security_group.http.id,
      aws_security_group.ssh.id,
    ]
  }

  monitoring {
    enabled = false
  }

  block_device_mappings {
    device_name = "/dev/xvda"
    ebs {
      volume_size           = var.root_volume_size
      encrypted             = true
      delete_on_termination = true
    }
  }

  metadata_options {
    # IMDSv2 required + hop_limit=1 closes the SSRF-to-instance-role
    # path. All curls in user-data.sh.tftpl and jenkins-graceful-stop.sh
    # negotiate an IMDSv2 token (PUT /latest/api/token) before reading
    # any metadata path. install-master-observability.sh uses the AWS
    # CLI which already speaks IMDSv2 via SDK.
    http_tokens                 = "required"
    http_put_response_hop_limit = 1
    http_endpoint               = "enabled"
  }

  # Runtime-spawned instances/volumes never see provider default_tags, so the
  # reaper pair + team must be re-asserted here (docs/runbooks/cleanup-reapers.md).
  tag_specifications {
    resource_type = "instance"
    tags = {
      Name              = var.short_name
      "iit-billing-tag" = var.short_name
      "PerconaKeep"     = "True"
      team              = var.team
    }
  }
  tag_specifications {
    resource_type = "volume"
    tags = {
      Name              = var.short_name
      "iit-billing-tag" = var.short_name
      "PerconaKeep"     = "True"
      team              = var.team
      "snapshot-policy" = "jenkins-master"
    }
  }

  user_data = base64encode(local.user_data_rendered)

  # Spot-survivability Phase 0: ignore_changes = [user_data] lifted now that the CF
  # stack jenkins-ps3 is gone (deleted 2026-05-19 via update-stack with
  # DeletionPolicy: Retain on all 24 resources, then plain delete-stack).
  # Future userdata edits (e.g. Phase 3 graceful spot-interrupt drain)
  # now flow through Terraform and produce a launch-template version bump
  # without instance churn; the SpotFleet picks up the new LT version on
  # the next replacement cycle.
}

resource "aws_spot_fleet_request" "master" {
  count                               = var.purchasing_option == "spot" ? 1 : 0
  iam_fleet_role                      = aws_iam_role.spot_fleet[0].arn
  target_capacity                     = 1
  spot_price                          = var.spot_price
  allocation_strategy                 = var.allocation_strategy
  excess_capacity_termination_policy  = "Default"
  replace_unhealthy_instances         = true
  terminate_instances_with_expiration = false
  fleet_type                          = "maintain"

  # Spot-survivability Phase 5: launch a replacement on EC2 Spot rebalance
  # recommendation. AWS issues rebalance recommendations minutes to
  # hours before the 2-min interruption notice; on receipt the fleet
  # launches a new instance proactively. We use `launch` (not
  # `launch-before-terminate`) for two reasons:
  #   1. The hashicorp/aws provider's aws_spot_fleet_request resource
  #      does not surface `termination_delay`, which AWS requires when
  #      using `launch-before-terminate` (the field is supported on
  #      the newer aws_ec2_fleet resource but not the legacy SpotFleet
  #      one we use here).
  #   2. Phase 3 graceful-stop.sh already handles the 2-min interrupt
  #      window, so we only need the proactive replacement signal, not
  #      AWS-managed termination of the old instance.
  # Old instance keeps running until the regular spot interruption
  # notice (or manual termination); Phase 3 drains it cleanly. Brief
  # double-spot cost during the overlap is acceptable for the canary.
  # Revisit `launch-before-terminate` when ps3 migrates to
  # aws_ec2_fleet (AWS-recommended successor to SpotFleet).
  spot_maintenance_strategies {
    capacity_rebalance {
      replacement_strategy = "launch"
    }
  }

  launch_template_config {
    launch_template_specification {
      id = aws_launch_template.master.id
      # $Latest symbolic version: SpotFleet always picks up the freshest
      # LT version on the next replacement. Previously this was set to
      # `aws_launch_template.master.latest_version` which TF resolved to
      # a literal number at plan time, then `ignore_changes` below
      # blocked updates -- so SpotFleet stayed pinned to the v at last
      # apply and userdata edits never landed (the Phase 3 rollout surfaced
      # this when LT bumped 99 -> 101 but SpotFleet still referenced 99).
      version = "$Latest"
    }

    dynamic "overrides" {
      for_each = toset(var.spot_instance_types)
      content {
        instance_type     = overrides.value
        availability_zone = data.aws_availability_zones.available.names[var.az_index]
      }
    }
  }

  lifecycle {
    # load_balancers / target_group_arns are optional+computed attributes
    # the AWS provider re-evaluates as "known after apply" on import,
    # which would otherwise force replacement of the live SpotFleet.
    # `launch_template_config` is NOT in the ignore list because we now
    # use $Latest (constant); the only diffs would be deliberate operator
    # changes to instance-type overrides etc., which should apply.
    ignore_changes = [load_balancers, target_group_arns]
  }
}

# On-demand path. Reuses the same launch template (user-data, networking,
# IAM, EBS attachment, EIP reassociation are identical). Single instance,
# no autoscaling: master state lives on the persistent EBS data volume so
# manual replacement is acceptable for the rare hardware-failure case.
resource "aws_instance" "master" {
  count             = var.purchasing_option == "on-demand" ? 1 : 0
  availability_zone = data.aws_availability_zones.available.names[var.az_index]

  launch_template {
    id      = aws_launch_template.master.id
    version = "$Latest"
  }

  # On-demand override of the launch template's instance_type. The LT's
  # instance_type field is consumed by the SpotFleet path; here we pin
  # the single instance to a stable size.
  instance_type = var.on_demand_instance_type

  # Trivy AWS-0131: root EBS encryption. Only affects the on-demand path
  # (count gated on purchasing_option); SpotFleet path is unchanged. AMI
  # default size/type inherit (volume_size/volume_type unset).
  root_block_device {
    encrypted   = true
    volume_size = var.root_volume_size

    # Provider default_tags reach the root volume, but the instance-level
    # identity tags (Name/iit-billing-tag/team) do not, so assert them here.
    tags = {
      Name              = var.short_name
      "iit-billing-tag" = var.short_name
      team              = var.team
    }
  }

  tags = {
    Name              = var.short_name
    "iit-billing-tag" = var.short_name
    team              = var.team
  }

  lifecycle {
    # AMI is owned by the launch template ($Latest); ignore drift here so
    # an LT version bump (userdata edit) doesn't force instance replacement.
    ignore_changes = [ami, user_data, user_data_base64, launch_template[0].version]
  }
}
