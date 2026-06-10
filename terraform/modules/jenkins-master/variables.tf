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
  description = "Instance types for the SpotFleet Overrides. Only consulted when purchasing_option = \"spot\"; pass [] for on-demand masters."
  type        = list(string)
  default     = []
}

variable "ssh_key_engineers" {
  description = "Engineer slugs whose public keys are fetched from percona.com/get/engineer/KEY/<slug>.pub at boot."
  type        = list(string)
}

variable "master_profile" {
  description = "User-data profile selector. Only \"eks_observability\" is supported; the slot exists so future variants (in-cluster master, alternate observability path) can be gated cleanly."
  type        = string
  validation {
    condition     = contains(["eks_observability"], var.master_profile)
    error_message = "master_profile must be \"eks_observability\"."
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
  description = "EBS volume type. Defaults to gp3 (3000 IOPS / 125 MB/s baseline, online modify from gp2 supported)."
  type        = string
  default     = "gp3"
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
  description = "Create the public A record. ps3=false (external-dns owns it)."
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

variable "init_groovy_hooks" {
  description = "init.groovy.d files fetched at boot, as a map of filename => pinned raw URL. Makes the master's wiring declarative and self-heals a fresh-volume rebuild (closes the bootstrap gap where only the carried-forward EBS held the files). `jenkins iac deploy` stays the no-restart hot-reload path for live edits between boots. Pin refs to commits/tags, never floating branches, so a boot cannot pull unreviewed HEAD. Empty skips. Kept for masters not yet moved to `init_groovy_files`."
  type        = map(string)
  default     = {}
}

variable "init_groovy_files" {
  description = "init.groovy.d files delivered via a module-created, per-master regional S3 bucket, as a map of filename => FILE CONTENT (the root passes file(...)). Supersedes init_groovy_hooks for masters where the repo is the source of truth: Terraform creates `<short_name>-init-config` (private, versioned), uploads each file as an s3 object keyed `init.groovy.d/<filename>`, grants the master role s3:GetObject + s3:ListBucket on it, and the boot path `aws s3 cp`s each file into /mnt/$JENKINS_HOST/init.groovy.d/. The upload runs with Terraform's AWS creds, not the master's. Co-located in the master's region so it transits the existing S3 gateway VPC endpoint (no NAT egress). A failed fetch warns but never blocks boot. `jenkins iac deploy` stays the no-restart hot-reload path for live edits between boots. Empty skips."
  type        = map(string)
  default     = {}
}

variable "init_groovy_template_files" {
  description = "init.groovy.d files rendered with templatefile() before upload, as a map of filename => .tftpl path. Template variables: subnet_by_az_name (AZ name => subnet id, from this module's subnets) and vpc_id. Lets netMap subnet IDs in cloud.groovy come from state instead of hand-edited literals, so a VPC or subnet replacement re-renders the S3 object in the same apply. Rendered entries merge OVER init_groovy_files on filename collision. Escape literal Groovy GStrings as $${...} in the template. Empty skips."
  type        = map(string)
  default     = {}
}

variable "init_groovy_sync_schedule" {
  description = "SSM schedule expression (e.g. rate(30 minutes)). When set and init.groovy.d S3 delivery is active, an SSM State Manager association periodically syncs the init-config bucket into /mnt/<hostname>/init.groovy.d on the master, so post-boot S3 changes (e.g. a re-rendered netMap) reach the EBS copy without an instance replacement and a JVM restart always loads current canonical. Additive sync, no deletes. Null disables."
  type        = string
  default     = null
}

variable "tags" {
  description = "Per-master tag overrides. The module-set keys (Name, iit-billing-tag, team) win over this map."
  type        = map(string)
  default     = {}
}

variable "team" {
  description = "Owning product team, recorded in the `team` tag on every resource this module creates and every instance/volume it spawns at runtime. Allowed values: the Owner set enforced by scripts/check_conventions.py."
  type        = string
  default     = "platform"
}

variable "purchasing_option" {
  description = "Master purchasing model. \"spot\" uses the SpotFleet path (default; preserves ps3 behaviour). \"on-demand\" provisions a single aws_instance and skips the SpotFleet path, for masters where reclaim-immunity justifies the cost premium (ps80 first)."
  type        = string
  default     = "spot"
  validation {
    condition     = contains(["spot", "on-demand"], var.purchasing_option)
    error_message = "purchasing_option must be \"spot\" or \"on-demand\"."
  }
}

variable "on_demand_instance_type" {
  description = "Instance type for the on-demand master. Only consulted when purchasing_option = \"on-demand\". Default sized for the observed master CPU footprint (p95 < 2% on 4 vCPU)."
  type        = string
  default     = "c7i-flex.large"
}

variable "launch_template_name" {
  description = "Override the master launch template name. null derives `<SHORT>MasterTemplate` (preserves ps3). Set a distinct value where a same-named CFN launch template must coexist during a CFN->TF cutover (ps80's CFN JMasterTemplate is literally `PS80MasterTemplate`)."
  type        = string
  default     = null
}

variable "root_volume_size" {
  description = "Root EBS volume size in GiB. The al2023-minimal AMI defaults to 2 GiB, which fills during bootstrap (OS + java + jenkins + openresty + dnf cache) and breaks the observability install. JENKINS_HOME lives on the separate data volume, so this covers OS + packages + logs only."
  type        = number
  default     = 50
}
