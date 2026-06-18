# Owner: platform
variable "region_label" {
  description = "Region name shown in the policy description (e.g. us-west-2)."
  type        = string
}

variable "execution_role_arn" {
  description = "ARN of the DLM execution role. IAM is global, so one role created in the root module serves every region."
  type        = string
}

variable "copy_target_region" {
  description = "Destination region for the cross-region snapshot copy. Null disables the copy."
  type        = string
  default     = null
}

variable "copy_target_cmk_arn" {
  description = "CMK ARN in the destination region used to re-encrypt cross-region copies. Required when copy_target_region is set."
  type        = string
  default     = null
}

variable "tags" {
  description = "Tags applied to the lifecycle policy resource."
  type        = map(string)
  default     = {}
}
