# Owner: platform
# Diversified x86_64 spot worker pool consumed by the Jenkins ec2-fleet plugin
# (EC2FleetCloud). Replaces a classic ec2-plugin SlaveTemplate whose single
# (instance type x AZ) spot pool is a reclaim blast-radius: one pool squeeze
# takes out every worker of the label at once. The ASG MixedInstancesPolicy
# lets AWS pick, per instance, the deepest of (types x subnets) pools.
#
# One module call per (master, pool). Terraform owns the pool shape (launch
# template, instance-type diversification, min/max, IAM); the ec2-fleet plugin
# owns DesiredCapacity at runtime, hence ignore_changes = [desired_capacity].
# Sibling of modules/jenkins-arm-fleet (the Graviton pool); kept separate
# because the bootstrap contract differs (distribution userland, SSH login
# shim) and the arm module's resource names are state contract surfaces.

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
  pool          = "${var.short_name}-${var.pool_name}"
  instance_tags = merge(local.fleet_tags, { Name = local.pool })
}

# Worker SG ingress is scoped to the master's own VPC CIDR (derived, not passed).
data "aws_vpc" "this" {
  id = var.vpc_id
}

resource "aws_security_group" "this" {
  name        = local.pool
  description = "Jenkins x86_64 spot fleet workers"
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

  tags = merge(local.fleet_tags, { Name = local.pool })
}

resource "aws_launch_template" "this" {
  name_prefix = "${local.pool}-"
  image_id    = var.ami_id
  key_name    = var.key_name

  # instance_type is intentionally omitted: the ASG mixed_instances_policy
  # overrides drive the type across the diversified pool.

  iam_instance_profile {
    name = var.worker_instance_profile_name
  }

  vpc_security_group_ids = [aws_security_group.this.id]

  # IMDSv2 required (trivy AWS-0130). hop_limit 2 keeps IMDS reachable from
  # containerized workloads on the worker while still blocking IMDSv1.
  metadata_options {
    http_endpoint               = "enabled"
    http_tokens                 = "required"
    http_put_response_hop_limit = 2
  }

  block_device_mappings {
    device_name = "/dev/xvda" # root
    ebs {
      volume_size           = var.root_volume_gb
      volume_type           = "gp3"
      delete_on_termination = true
    }
  }
  block_device_mappings {
    device_name = "/dev/xvdd" # /mnt build data
    ebs {
      volume_size           = var.data_volume_gb
      volume_type           = "gp3"
      delete_on_termination = true
    }
  }

  # Launch-template user_data is NOT auto-base64-encoded (unlike aws_instance).
  # Static bootstrap (no interpolation) so shell ${...} stays literal: use file().
  user_data = base64encode(file(var.user_data_file))

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
  name = local.pool

  min_size         = 0
  max_size         = var.max_size
  desired_capacity = 0

  # Span every supplied subnet (one per AZ) so the allocation strategy has the
  # most spot pools (instance type x AZ) to choose from.
  vpc_zone_identifier = var.subnet_ids
  capacity_rebalance  = true

  mixed_instances_policy {
    instances_distribution {
      on_demand_base_capacity                  = 0
      on_demand_percentage_above_base_capacity = 0 # 100% spot
      # price-capacity-optimized (AWS-recommended): capacity-optimized concentrated
      # the arm fleet into one pool (m7g.2xlarge us-west-2b, 2026-06-08), so one
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
# ASG. Describe verbs need "*" (no resource-level support); the write verbs
# are scoped to this pool's ASG so the master role cannot resize or terminate
# unrelated ASGs. Attaches a named inline policy, so it coexists cleanly with
# the arm fleet's sibling policy on the same role.
data "aws_iam_policy_document" "autoscaling" {
  statement {
    sid    = "Ec2FleetPluginAutoScalingRead"
    effect = "Allow"
    actions = [
      "autoscaling:DescribeAutoScalingGroups",
      "autoscaling:DescribeScalingActivities",
    ]
    resources = ["*"]
  }

  statement {
    sid    = "Ec2FleetPluginAutoScalingWrite"
    effect = "Allow"
    actions = [
      "autoscaling:UpdateAutoScalingGroup",
      "autoscaling:SetDesiredCapacity",
      "autoscaling:TerminateInstanceInAutoScalingGroup",
    ]
    resources = ["arn:aws:autoscaling:*:*:autoScalingGroup:*:autoScalingGroupName/${local.pool}"]
  }
}

resource "aws_iam_role_policy" "autoscaling" {
  name   = local.pool
  role   = var.master_role_name
  policy = data.aws_iam_policy_document.autoscaling.json
}
