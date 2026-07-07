# Inputs are deliberately minimal so the module attaches to both Terraform-
# managed masters (feed module.<master>.* outputs) and masters not managed by
# `jenkins-master` (feed existing resource IDs via data sources / literals).

variable "short_name" {
  description = "Master short name, e.g. jenkins-ps3. Prefixes fleet resource names + sets the cleanup-safe iit-billing-tag."
  type        = string
}

variable "vpc_id" {
  description = "VPC the fleet workers launch into (the master's VPC). The worker SG CIDR is derived from it."
  type        = string
}

variable "subnet_ids" {
  description = "Subnets (ideally one per AZ) for the ASG. More AZs => more spot pools => higher capacity-optimized fulfillment."
  type        = list(string)
}

variable "worker_instance_profile_name" {
  description = "EC2 instance profile for fleet workers (reuse the master's worker profile)."
  type        = string
}

variable "master_role_name" {
  description = "Master IAM role name. The ec2-fleet plugin runs under it, so the autoscaling policy is attached here."
  type        = string
}

variable "key_name" {
  description = "EC2 key pair for workers; the ec2-fleet plugin SSH launcher authenticates with the matching private-key credential in Jenkins."
  type        = string
}

variable "instance_types" {
  description = "Graviton instance types for the diversified pool. Use >= 3 same-size types (e.g. [\"m8g.2xlarge\", \"m7g.2xlarge\", \"m6g.2xlarge\"]) so capacity-optimized yields a healthy Spot Placement Score (~9 vs ~3 single-type)."
  type        = list(string)
  validation {
    condition     = length(var.instance_types) > 0
    error_message = "instance_types must list at least one Graviton type (>= 3 recommended)."
  }
}

variable "max_size" {
  description = "Max instances in the ASG. The ec2-fleet plugin scales DesiredCapacity within [0, this]."
  type        = number
  default     = 2
}

variable "data_volume_gb" {
  description = "Size (GiB) of the /mnt build + docker data volume on fleet workers."
  type        = number
  default     = 120
}

variable "ami_id" {
  description = "Override the worker AMI. null resolves the latest Amazon Linux 2 arm64 AMI in the module's region."
  type        = string
  default     = null
}

variable "tickets" {
  description = "Tracking tickets (comma-separated), recorded in the `tickets` tag rather than in resource names."
  type        = string
  default     = ""
}

variable "tags" {
  description = "Extra tags merged onto all fleet resources (and propagated to instances). Module-set keys (iit-billing-tag, team, PerconaKeep) win over this map."
  type        = map(string)
  default     = {}
}

variable "team" {
  description = "Owning product team, recorded in the `team` tag on every resource this module creates and every instance/volume it spawns at runtime. Per-master values are validated by scripts/check_conventions.py against its instance map; cloud tags team=cloud-cd to stay outside the cloud team's orphan-cleanup lambdas (docs/runbooks/cleanup-reapers.md)."
  type        = string
  default     = "platform"
}

variable "extra_ssh_cidrs" {
  description = "Additional CIDRs allowed to SSH (:22) to the workers, e.g. the in-cluster controller's EKS VPC CIDR for the ec2-fleet privateIpUsed connection over VPC peering."
  type        = list(string)
  default     = []
}
