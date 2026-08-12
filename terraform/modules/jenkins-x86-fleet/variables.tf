# Inputs are deliberately minimal so the module attaches to both Terraform-
# managed masters (feed module.<master>.* outputs) and still-CloudFormation
# masters (feed existing resource IDs via data sources / literals).

variable "short_name" {
  description = "Master short name, e.g. jenkins-ps80. Prefixes fleet resource names + sets the cleanup-safe iit-billing-tag."
  type        = string
}

variable "pool_name" {
  description = "Distinguishes this pool from other x86 fleets on the same master; part of every resource name (ASG: <short_name>-<pool_name>)."
  type        = string
}

variable "vpc_id" {
  description = "VPC the fleet workers launch into (the master's VPC). The worker SG CIDR is derived from it."
  type        = string
}

variable "subnet_ids" {
  description = "Subnets (ideally one per AZ) for the ASG. More AZs => more spot pools => higher fulfillment."
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

variable "ami_id" {
  description = "Worker AMI. Required: the OS userland is part of the label contract (e.g. the min-bookworm pool must run the same Debian 12 AMI as the classic template it replaces)."
  type        = string
}

variable "user_data_file" {
  description = "Bootstrap script shipped as launch-template user_data, relative to the caller (distribution-specific: package manager, SSH login user shim)."
  type        = string
}

variable "instance_types" {
  description = "Same-size x86_64 instance types for the diversified pool. Use >= 3 (e.g. [\"m6a.2xlarge\", \"m6i.2xlarge\", \"m5a.2xlarge\"]) so the allocation strategy has enough spot pools."
  type        = list(string)
  validation {
    condition     = length(var.instance_types) > 0
    error_message = "instance_types must list at least one x86_64 type (>= 3 recommended)."
  }
}

variable "max_size" {
  description = "Max instances in the ASG. The ec2-fleet plugin scales DesiredCapacity within [0, this]."
  type        = number
  default     = 2
}

variable "root_volume_gb" {
  description = "Size (GiB) of the root volume. Match the classic template the pool replaces; runtime scratch (/tmp, package caches) lives here."
  type        = number
  default     = 8
}

variable "data_volume_gb" {
  description = "Size (GiB) of the /mnt data volume on fleet workers."
  type        = number
  default     = 30
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
  description = "Owning product team, recorded in the `team` tag on every resource this module creates and every instance/volume it spawns at runtime. Per-master values are validated by scripts/check_conventions.py against its instance map."
  type        = string
  default     = "platform"
}

variable "extra_ssh_cidrs" {
  description = "Additional CIDRs allowed to SSH (:22) to the workers, e.g. the in-cluster controller's EKS VPC CIDR for the ec2-fleet privateIpUsed connection over VPC peering."
  type        = list(string)
  default     = []
}
