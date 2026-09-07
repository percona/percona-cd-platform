# Owner: platform
# Diversified ARM (Graviton) spot worker pool consumed by the Jenkins ec2-fleet
# plugin (EC2FleetCloud): automatic AWS Graviton fallback for the docker-aarch64
# workload when Hetzner ARM (CAX) capacity is unavailable.
#
# One module call per master. Terraform owns the pool shape (launch template,
# instance-type diversification, min/max, IAM); the ec2-fleet plugin owns
# DesiredCapacity at runtime, hence ignore_changes = [desired_capacity].

locals {
  # iit-billing-tag MUST be the worker form ("<short_name>-worker"), never the
  # bare master tag: the jenkins-endpoint-reconciler matches the master by exact
  # iit-billing-tag=<short_name>, so a worker sharing it becomes a candidate for
  # the master EndpointSlice and can blackhole the master's ingress (503). The
  # worker form also matches the existing ec2-plugin workers and is cleanup-safe.
  # Module-set keys merge LAST: a caller map must never clobber the worker
  # billing form, and runtime-spawned instances/volumes need PerconaKeep + team
  # re-asserted (no provider default_tags on ASG-launched resources).
  fleet_tags = merge(
    var.tags,
    var.tickets == "" ? {} : { tickets = var.tickets },
    {
      "iit-billing-tag" = "${var.short_name}-worker"
      "PerconaKeep"     = "True"
      team              = var.team
    },
  )
  instance_tags = merge(local.fleet_tags, { Name = "${var.short_name}-arm-worker" })
}

# Worker SG ingress is scoped to the master's own VPC CIDR (derived, not passed).
data "aws_vpc" "this" {
  id = var.vpc_id
}

# Latest Amazon Linux 2023 (kernel 6.1) arm64 AMI in the module's region
# (override via var.ami_id). AL2 is past end of support and its kernel 4.14
# lacks openat2, which current Ubuntu 26.04 containers require.
# owners is pinned per the provider's supply-chain guidance for most_recent.
data "aws_ami" "arm64" {
  count       = var.ami_id == null ? 1 : 0
  most_recent = true
  owners      = ["amazon"]

  filter {
    name   = "name"
    values = ["al2023-ami-2023.*-kernel-6.1-arm64"]
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
  vpc_id      = var.vpc_id

  ingress {
    description = "SSH from the master (ec2-fleet plugin launcher) + any extra controller CIDRs"
    from_port   = 22
    to_port     = 22
    protocol    = "tcp"
    cidr_blocks = concat([data.aws_vpc.this.cidr_block], var.extra_ssh_cidrs)
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

  # instance_type is intentionally omitted: the ASG mixed_instances_policy
  # overrides drive the type across the diversified Graviton pool.

  iam_instance_profile {
    name = var.worker_instance_profile_name
  }

  vpc_security_group_ids = [aws_security_group.this.id]

  # IMDSv2 required (trivy AWS-0130). hop_limit 2 keeps IMDS reachable from
  # the Docker build containers on the worker while still blocking IMDSv1.
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

  # Launch-template user_data is NOT auto-base64-encoded (unlike aws_instance).
  # Static bootstrap (no interpolation) so shell ${...} stays literal: use file().
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

  # Span every supplied subnet (one per AZ) so capacity-optimized has the most
  # spot pools (instance type x AZ) to choose from.
  vpc_zone_identifier = var.subnet_ids
  capacity_rebalance  = true

  mixed_instances_policy {
    instances_distribution {
      on_demand_base_capacity                  = 0
      on_demand_percentage_above_base_capacity = 0 # 100% spot
      # price-capacity-optimized (AWS-recommended): capacity-optimized concentrated
      # the whole fleet into one pool (m7g.2xlarge us-west-2b, 2026-06-08), so one
      # pool squeeze took out three workers in 25 minutes.
      spot_allocation_strategy = "price-capacity-optimized"
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

  # The ec2-fleet plugin owns DesiredCapacity AND sets protect_from_scale_in=true
  # at runtime (so it, not the ASG's own scale-in, controls instance termination).
  # Ignore both runtime-owned attributes or every apply fights the plugin
  # (protect_from_scale_in true->false drift). min/max stay Terraform-managed
  # guardrails, so they are deliberately NOT ignored.
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

# Lets the ec2-fleet plugin (running under the master role) scale and reap the
# ASG. Read verbs + the three write verbs the plugin issues. Attaches a named
# inline policy, so it coexists cleanly even on a CloudFormation-managed role.
data "aws_iam_policy_document" "autoscaling" {
  statement {
    sid    = "Ec2FleetPluginAutoScaling"
    effect = "Allow"
    actions = [
      "autoscaling:DescribeAutoScalingGroups",
      "autoscaling:DescribeScalingActivities",
      "autoscaling:UpdateAutoScalingGroup",
      "autoscaling:SetDesiredCapacity",
      "autoscaling:TerminateInstanceInAutoScalingGroup",
    ]
    resources = ["*"]
  }
}

resource "aws_iam_role_policy" "autoscaling" {
  name   = "${var.short_name}-arm-fleet"
  role   = var.master_role_name
  policy = data.aws_iam_policy_document.autoscaling.json
}
