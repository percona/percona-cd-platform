variable "short_name" {
  description = "Master short name, e.g. jenkins-ps3. Prefixes resource names + sets the iit-billing-tag."
  type        = string
}

variable "vpc_cidr" {
  description = "VPC CIDR. Must match the existing master VPC being moved in (ps3: 10.181.0.0/22) so the move is zero-diff."
  type        = string
}

variable "cache_bucket_name" {
  description = "Worker build cache S3 bucket. null disables the worker S3 IAM statement. ps3 passes ps-build-cache."
  type        = string
  default     = null
}

variable "key_name" {
  description = "EC2 key pair for workers; the ec2-fleet plugin SSH launcher uses the matching Jenkins credential."
  type        = string
  default     = "percona-jenkins"
}

variable "instance_types" {
  description = "Graviton instance types for the diversified pool (>= 3 same-size types recommended)."
  type        = list(string)
  validation {
    condition     = length(var.instance_types) > 0
    error_message = "instance_types must list at least one Graviton type."
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
  description = "Override the worker AMI. null resolves the latest Amazon Linux 2 arm64 AMI in the region."
  type        = string
  default     = null
}

variable "tickets" {
  description = "Tracking tickets (comma-separated), recorded in the tickets tag."
  type        = string
  default     = ""
}

variable "tags" {
  description = "Extra tags merged onto all resources. Module-set keys (Name, iit-billing-tag, team, PerconaKeep) win over this map."
  type        = map(string)
  default     = {}
}

variable "team" {
  description = "Owning product team, recorded in the `team` tag on every resource this module creates and every instance/volume it spawns at runtime. Allowed values: the Owner set enforced by cdpctl conventions."
  type        = string
  default     = "platform"
}

variable "extra_ssh_cidrs" {
  description = "Additional CIDRs allowed to SSH (:22) to workers, e.g. the in-cluster controller's EKS VPC CIDR for the ec2-fleet privateIpUsed connection over VPC peering."
  type        = list(string)
  default     = []
}
