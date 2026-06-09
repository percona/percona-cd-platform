# Owner: platform
# jenkins-arm-standalone: the ps3 ARM Graviton fleet PLUS its own network + worker
# IAM substrate, re-parented off the (being-retired) jenkins-master module.ps3.
# Owns the VPC (10.181.0.0/22), subnets, IGW, route table, S3 gateway endpoint,
# the worker IAM role/instance-profile, and the ARM SG/LT/ASG.
#
# The in-cluster ps3-k8s controller drives the ASG over the EKS<->ps3 peering via
# its EKS Pod Identity role (aws_iam_policy.jenkins_controller already grants
# autoscaling on jenkins-*-arm-*), so this module deliberately does NOT recreate
# the old master-role autoscaling inline policy.
#
# Resource LOCAL NAMES match their source modules on purpose: the substrate +
# worker-IAM names match jenkins-master so a `moved {}` block relocates them with
# zero diff; the ARM SG/LT/ASG names match jenkins-arm-fleet so they stay in place
# when module.ps3_arm_fleet's source flips to this module.

locals {
  base_tags = merge(
    {
      Name              = var.short_name
      "iit-billing-tag" = var.short_name
    },
    var.tags,
  )

  worker_name = "${var.short_name}-worker"

  # CF used AZ index 1 (B) + 2 (C); matches the ps3 master subnets being moved in.
  subnet_indices = { "b" = 1, "c" = 2 }

  fleet_tags = merge(
    { "iit-billing-tag" = "${var.short_name}-worker" },
    var.tags,
    var.tickets == "" ? {} : { tickets = var.tickets },
  )
  instance_tags = merge(local.fleet_tags, { Name = "${var.short_name}-arm-worker" })
}

data "aws_availability_zones" "available" {
  state = "available"
}

data "aws_region" "current" {}

# ---------- network substrate (moved from module.ps3) ----------

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

data "aws_iam_policy_document" "s3_endpoint" {
  statement {
    effect = "Allow"
    principals {
      type        = "*"
      identifiers = ["*"]
    }
    actions = [
      "s3:ListBucket",
      "s3:GetObject",
      "s3:GetObjectAcl",
      "s3:PutObject",
      "s3:PutObjectAcl",
      "s3:DeleteObject",
      "s3:AbortMultipartUpload",
    ]
    resources = ["*"]
  }
}

# ---------- worker IAM (moved from module.ps3) ----------

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

resource "aws_iam_role" "worker" {
  name               = local.worker_name
  path               = "/"
  assume_role_policy = data.aws_iam_policy_document.ec2_assume.json
  tags               = local.base_tags
}

# EC2 tag perms always; S3 cache only when caller passes cache_bucket_name.
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
}

resource "aws_iam_role_policy" "worker" {
  name   = local.worker_name
  role   = aws_iam_role.worker.id
  policy = data.aws_iam_policy_document.worker.json
}

resource "aws_iam_instance_profile" "worker" {
  name = local.worker_name
  path = "/"
  role = aws_iam_role.worker.name
}

# ---------- ARM Graviton spot fleet (stays at module.ps3_arm_fleet.*) ----------

# Latest Amazon Linux 2 arm64 AMI in the module's region (override via var.ami_id).
data "aws_ami" "arm64" {
  count       = var.ami_id == null ? 1 : 0
  most_recent = true
  owners      = ["amazon"]

  filter {
    name   = "name"
    values = ["amzn2-ami-hvm-*-arm64-gp2"]
  }
  filter {
    name   = "architecture"
    values = ["arm64"]
  }
  filter {
    name   = "state"
    values = ["available"]
  }
}

resource "aws_security_group" "this" {
  name        = "${var.short_name}-arm-worker"
  description = "Jenkins ARM Graviton spot workers"
  vpc_id      = aws_vpc.this.id

  ingress {
    description = "SSH from the master (ec2-fleet plugin launcher) + any extra controller CIDRs"
    from_port   = 22
    to_port     = 22
    protocol    = "tcp"
    cidr_blocks = concat([aws_vpc.this.cidr_block], var.extra_ssh_cidrs)
  }

  egress {
    from_port   = 0
    to_port     = 0
    protocol    = "-1"
    cidr_blocks = ["0.0.0.0/0"]
  }

  tags = merge(local.fleet_tags, { Name = "${var.short_name}-arm-worker" })
}

resource "aws_launch_template" "this" {
  name_prefix = "${var.short_name}-arm-"
  image_id    = coalesce(var.ami_id, try(data.aws_ami.arm64[0].id, null))
  key_name    = var.key_name

  # instance_type intentionally omitted: the ASG mixed_instances_policy overrides
  # drive the type across the diversified Graviton pool.

  iam_instance_profile {
    name = aws_iam_instance_profile.worker.name
  }

  vpc_security_group_ids = [aws_security_group.this.id]

  metadata_options {
    http_endpoint               = "enabled"
    http_tokens                 = "required"
    http_put_response_hop_limit = 2
  }

  block_device_mappings {
    device_name = "/dev/xvda" # root
    ebs {
      volume_size           = 8
      volume_type           = "gp3"
      delete_on_termination = true
    }
  }
  block_device_mappings {
    device_name = "/dev/xvdd" # /mnt build + docker data
    ebs {
      volume_size           = var.data_volume_gb
      volume_type           = "gp3"
      delete_on_termination = true
    }
  }

  user_data = base64encode(file("${path.module}/arm-worker-user-data.sh"))

  tag_specifications {
    resource_type = "instance"
    tags          = local.instance_tags
  }
  tag_specifications {
    resource_type = "volume"
    tags          = local.instance_tags
  }

  update_default_version = true
  lifecycle {
    create_before_destroy = true
  }
}

resource "aws_autoscaling_group" "this" {
  name = "${var.short_name}-arm-graviton"

  min_size         = 0
  max_size         = var.max_size
  desired_capacity = 0

  vpc_zone_identifier = [for s in aws_subnet.this : s.id]
  capacity_rebalance  = true

  mixed_instances_policy {
    instances_distribution {
      on_demand_base_capacity                  = 0
      on_demand_percentage_above_base_capacity = 0 # 100% spot
      spot_allocation_strategy                 = "capacity-optimized"
    }

    launch_template {
      launch_template_specification {
        launch_template_id = aws_launch_template.this.id
        version            = aws_launch_template.this.latest_version
      }

      dynamic "override" {
        for_each = var.instance_types
        content {
          instance_type = override.value
        }
      }
    }
  }

  # The ec2-fleet plugin owns DesiredCapacity and protect_from_scale_in at runtime.
  lifecycle {
    ignore_changes = [desired_capacity, protect_from_scale_in]
  }

  dynamic "tag" {
    for_each = local.instance_tags
    content {
      key                 = tag.key
      value               = tag.value
      propagate_at_launch = true
    }
  }
}
