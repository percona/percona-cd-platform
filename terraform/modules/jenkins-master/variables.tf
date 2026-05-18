# Required inputs are at the top. Defaults below match the canonical
# ps3-shaped master: AL2023, java-17, jenkins 2.541.3, master on :8080
# only, 100GiB gp2 in the region's second AZ, SpotFleet with
# capacityOptimized, SQS termination wired in, external-dns owning DNS.
# Per-master quirks (psmdb subnets/naming, pmm JWorkerUser, pxc plugin
# hook, etc.) are exposed as toggles rather than normalised during the
# CF -> TF flip; convergence is tracked separately.

# ----- Required -----

variable "hostname" {
  description = "Public FQDN, e.g. ps3.cd.percona.com. Drives JENKINS_HOST and the optional Route53 record."
  type        = string
}

variable "short_name" {
  description = "Tag + IAM resource short name, e.g. jenkins-ps3."
  type        = string
}

variable "vpc_cidr" {
  description = "Master VPC CIDR. Must be unique vs the EKS VPC (10.220.0.0/16) and any already-peered master VPC."
  type        = string
}

variable "ami_id" {
  description = "Master EC2 AMI (caller resolves per-region)."
  type        = string
}

variable "spot_instance_types" {
  description = "Instance types for the SpotFleet Overrides."
  type        = list(string)
}

variable "ssh_key_engineers" {
  description = "Engineer slugs whose public keys are fetched from percona.com/get/engineer/KEY/<slug>.pub at boot."
  type        = list(string)
}

variable "master_profile" {
  description = "User-data profile: \"eks_observability\" (PS-10945 path) or the stubs \"legacy_openresty\" / \"legacy_nginx12\" (error until implemented)."
  type        = string
  validation {
    condition     = contains(["eks_observability", "legacy_openresty", "legacy_nginx12"], var.master_profile)
    error_message = "master_profile must be one of: eks_observability, legacy_openresty, legacy_nginx12."
  }
}

# ----- Optional with sensible defaults -----

variable "master_key_name" {
  description = "Existing EC2 key pair for the master."
  type        = string
  default     = "percona-jenkins"
}

variable "az_index" {
  description = "AZ index for the master subnet + EBS. CF used 1 (B); keep at 1 unless pinned elsewhere."
  type        = number
  default     = 1
}

variable "ebs_size" {
  description = "JENKINS_HOME data volume size (GiB)."
  type        = number
  default     = 100
}

variable "ebs_type" {
  description = "EBS volume type. gp2 for most; pmm/ps80 use gp3; psmdb uses gp2 with 300 GiB."
  type        = string
  default     = "gp2"
}

variable "spot_price" {
  description = "Max spot bid (USD/hr)."
  type        = string
  default     = "0.15"
}

variable "allocation_strategy" {
  description = "SpotFleet strategy. capacityOptimized everywhere except pmm (lowestPrice)."
  type        = string
  default     = "capacityOptimized"
}

variable "cache_bucket_name" {
  description = "Worker build cache S3 bucket. null disables the worker S3 IAM policy (pxc/pxb)."
  type        = string
  default     = null
}

variable "extra_master_managed_policies" {
  description = "Extra AWS managed policy ARNs for the master role. SSM Managed Instance Core is baseline; this var is for additions."
  type        = list(string)
  default     = []
}

variable "extra_master_inline_policies" {
  description = "Extra inline policies for the master role. Each entry is { name, json }."
  type = list(object({
    name = string
    json = string
  }))
  default = []
}

variable "extra_http_ingress" {
  description = "Extra HTTP SG ingress. Each entry is { port, cidr }. ps3 uses [{8080, EKS-CIDR}]."
  type = list(object({
    port = number
    cidr = string
  }))
  default = []
}

variable "ssh_allowed_cidrs" {
  description = "Source CIDRs allowed on :22. Default is the 6-CIDR fleet baseline; pmm differs."
  type        = list(string)
  default = [
    "46.149.86.84/32",
    "54.214.47.252/32",
    "54.214.47.254/32",
    "176.37.55.60/32",
    "188.163.20.103/32",
    "213.159.239.48/32",
  ]
}

variable "create_eip" {
  description = "Create a stable EIP. ps3=false (ALB owns HTTPS, SSM owns SSH); non-cutover masters keep true. When false the SpotFleet picks a random public IPv4 each rotation and user-data skips associate-address."
  type        = bool
  default     = false
}

variable "create_route53_record" {
  description = "Create the public A record. ps3=false (external-dns owns it post-PS-10945)."
  type        = bool
  default     = false
}

variable "route53_zone_id" {
  description = "Hosted zone ID when create_route53_record=true."
  type        = string
  default     = "Z1H0AFAU7N8IMC"
}

variable "jenkins_package_version" {
  description = "Jenkins yum package version."
  type        = string
  default     = "2.541.3"
}

variable "create_termination_queue" {
  description = "Create SQS + EventBridge spot-interruption queue. psmdb=false."
  type        = bool
  default     = true
}

variable "worker_role_legacy_naming" {
  description = "Use legacy JSlave* names. Only psmdb."
  type        = bool
  default     = false
}

variable "extra_subnet_a" {
  description = "Create an extra subnet in AZ index 0. Only psmdb."
  type        = bool
  default     = false
}

variable "create_worker_user" {
  description = "Create an IAM User (JWorkerUser). Only pmm."
  type        = bool
  default     = false
}

variable "plugin_install_hook" {
  description = "Optional plugins.groovy URL fetched into init.groovy.d at boot (pxc). null skips."
  type        = string
  default     = null
}

variable "tags" {
  description = "Per-master tag overrides merged on top of provider default_tags."
  type        = map(string)
  default     = {}
}
