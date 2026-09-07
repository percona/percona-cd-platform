# Owner: platform
variable "aws_region" {
  description = "AWS region for the EKS cluster + all data-plane resources."
  type        = string
  default     = "us-east-1"
}

variable "aws_profile" {
  description = <<-EOT
    Optional AWS named profile. Leave empty to use the SDK default credential
    chain (env vars, EC2 instance profile, SSO, etc.). This var exists so
    contributors can override locally without editing the providers block.
  EOT
  type        = string
  default     = ""
}

variable "cluster_name" {
  description = "EKS cluster name. Matches the repo name."
  type        = string
  default     = "percona-ci-platform"
}

variable "cluster_version" {
  description = <<-EOT
    EKS Kubernetes minor version. Track standard support only — picking a version
    in extended support incurs the paid extended-support fee.
    Verify with `aws eks describe-cluster-versions`.
  EOT
  type        = string
  default     = "1.35"
}

variable "vpc_cidr" {
  description = "Cluster VPC CIDR. Avoid the Jenkins-VPC ranges 10.144/.145/.155/.156/.157/.158/.159/.160/.161/.166/.177/.179/.181/.188/.199 (multi-region masters and fleets)."
  type        = string
  default     = "10.220.0.0/16"
}

variable "argocd_hostname" {
  description = "Public hostname for ArgoCD UI. external-dns publishes the ALB alias."
  type        = string
  default     = "argo.cd.percona.com"
}

variable "grafana_hostname" {
  description = "Public hostname for Grafana UI."
  type        = string
  default     = "grafana.cd.percona.com"
}

variable "route53_zone_name" {
  description = "Public hosted zone for *.cd.percona.com. The zone ID is data-resolved (data.aws_route53_zone.main)."
  type        = string
  default     = "cd.percona.com"
}

variable "access_entries" {
  description = <<-EOT
    Additional EKS access entries, merged ON TOP of the committed baseline
    (local.base_access_entries in eks.tf, the dynamically resolved IAM
    Identity Center AdministratorAccess role). Day-to-day cluster admin needs
    no entry here. Use for break-glass or extra principals only. The public
    repo never carries IAM ARNs, so populate this in
    `terraform/local.auto.tfvars` (gitignored) when needed. Example:

    access_entries = {
      breakglass = {
        principal_arn = "arn:aws:iam::<account>:user/<user>"
        policy_associations = {
          admin = {
            policy_arn   = "arn:aws:eks::aws:cluster-access-policy/AmazonEKSClusterAdminPolicy"
            access_scope = { type = "cluster" }
          }
        }
      }
    }

    On key collision the tfvars entry wins (merge order).
    docs/eks-hardening.md item #1.
  EOT
  type = map(object({
    principal_arn = string
    policy_associations = optional(map(object({
      policy_arn = string
      access_scope = object({
        type       = string
        namespaces = optional(list(string))
      })
    })), {})
    kubernetes_groups = optional(list(string), [])
    type              = optional(string, "STANDARD")
  }))
  default = {}
}

variable "api_public_access_cidrs" {
  description = <<-EOT
    Additive override for the EKS public API allowlist. The baseline lives in
    SSM parameter /<cluster_name>/allowlist/eks-api (StringList, see
    terraform/allowlists.tf and docs/runbooks/eks-api-access.md),
    amended via `just allowlist-set`. Leave this empty in normal operation.
    Set it in `terraform/local.auto.tfvars` (gitignored) only for emergency
    access while the parameter catches up. The non-empty guarantee lives on
    the parameter's postcondition.

    docs/eks-hardening.md item #2.
  EOT
  type        = list(string)
  default     = []

  validation {
    condition     = !contains(var.api_public_access_cidrs, "0.0.0.0/0")
    error_message = "0.0.0.0/0 is forbidden by the hardening baseline (docs/eks-hardening.md #2)."
  }
}

variable "addon_versions" {
  description = <<-EOT
    Per-addon version pins for the EKS managed addons (vpc-cni, kube-proxy,
    coredns, eks-pod-identity-agent, aws-ebs-csi-driver). Empty map = let the
    EKS API resolve to the channel default for var.cluster_version. After
    first apply, populate via local.auto.tfvars and bump explicitly per
    docs/eks-hardening.md item #5 — never tracking "latest".

    Look values up with:
      aws eks describe-addon-versions \
        --kubernetes-version 1.35 \
        --addon-name <name> \
        --query 'addons[].addonVersions[?compatibilities[?defaultVersion==`true`]].addonVersion'
  EOT
  type        = map(string)
  default     = {}
}

variable "monitoring_az" {
  description = "Single AZ for stateful workloads (Prometheus, Jenkins masters). EBS is zonal."
  type        = string
  default     = "us-east-1a"
}

variable "authentik_hostname" {
  description = <<-EOT
    Public hostname for the Authentik bridge (SAML SP to Duo, OIDC IdP
    to Grafana / future Jenkins masters / ArgoCD UI). external-dns
    publishes the ALB alias when the chart Ingress is admitted.
  EOT
  type        = string
  default     = "auth.cd.percona.com"
}

variable "authentik_saml_enabled" {
  description = <<-EOT
    Toggles Authentik's SAML SP source for Duo (HD-30780). Default
    false so the cluster can boot before the SP cert/key + IdP metadata
    are populated. When true:

      - SP cert + private_key are fetched from AWS Secrets Manager
        (paths `$${cluster_name}/authentik/saml/{certificate,private_key}`)
        via External Secrets Operator into the `authentik-saml` Secret
        in the authentik namespace.
      - IdP metadata XML is read from SSM Parameter Store
        (`/$${cluster_name}/authentik/saml/idp_metadata`) at apply time
        and rendered into a ConfigMap mounted at
        /etc/authentik/saml-idp/idp-metadata.xml.

    Replaces the prior var.grafana_saml_enabled — Grafana OSS lacks
    SAML support, so the SAML SP role moved to Authentik (which
    front-doors Grafana via OIDC). See
    docs/adr/0012-authentik-saml-oidc-bridge.md for the architecture
    rationale.
  EOT
  type        = bool
  default     = false
}

variable "authentik_secret_breakglass_arns" {
  description = <<-EOT
    Extra IAM principal ARN patterns allowed to GetSecretValue on the
    Authentik secret bundle besides the ESO pod-identity role
    (aws:PrincipalArn StringNotLike patterns, `*` wildcards allowed).
    Keep empty in normal operation. The deny in
    aws_secretsmanager_secret_policy.authentik_config covers
    GetSecretValue only, so an administrator can always lift a lockout
    by editing the resource policy itself.
  EOT
  type        = list(string)
  default     = []
}

variable "lgtm_push_hostnames" {
  description = <<-EOT
    Public ALB hostnames for external pushers (Jenkins masters with
    prometheus-plugin + Hetzner cloud plugin metrics, etc.) to send
    metrics/logs/traces into Mimir/Loki/Tempo via the in-cluster Alloy
    gateway. Default values land under cd.percona.com — change only if
    a different DNS shape is needed.
  EOT
  type = object({
    mimir = string
    loki  = string
    tempo = string
  })
  default = {
    mimir = "mimir-push.cd.percona.com"
    loki  = "loki-push.cd.percona.com"
    tempo = "tempo-push.cd.percona.com"
  }
}

variable "tags" {
  description = <<-EOT
    Default tags for every taggable AWS resource. Two of these are required by
    the percona-dev-admin account's cleanup reapers (this repo,
    ec2-cleanup.tf / volume-cleanup.tf) — do not drop them:

    - `iit-billing-tag` — the EC2 reaper terminates instances missing a valid
      value after 10 minutes (numeric values are a unix-epoch expiry; anything
      else is a permanent category).
    - `PerconaKeep` — the volume reaper deletes any `available` EBS volume
      daily unless this tag is present (capital P, capital K).

    EBS volumes provisioned by the in-cluster aws-ebs-csi-driver pick up
    these tags via StorageClass `parameters.tagSpecification_*` (see
    resources/addons/storageclass-gp3/templates/storageclasses.yaml).
  EOT
  type        = map(string)
  default = {
    "iit-billing-tag" = "percona-ci-platform"
    "PerconaKeep"     = "True"
    "managed-by"      = "opentofu"
    "repo"            = "github.com/Percona/percona-cd-platform"
    "team"            = "platform"
  }
}

variable "ppg_ami_factory_region" {
  description = "AWS region the PPG AMI factory bakes in (matches the PG molecule region). Distinct from the cluster region."
  type        = string
  default     = "eu-central-1"
}

variable "ppg_ami_factory_subject_claims" {
  description = "GitHub Actions sub claims allowed to assume the PPG AMI-factory role. Production is master-only on Percona-Lab/jenkins-pipelines. A temporary fork subject may be supplied at apply time for end-to-end testing, then removed."
  type        = list(string)
  default     = ["repo:Percona-Lab/jenkins-pipelines:ref:refs/heads/master"]
}

variable "ppg_hcloud_factory_subject_claims" {
  description = "GitHub Actions sub claims allowed to assume the PPG Hetzner-factory role. Master-only on Percona-Lab/jenkins-pipelines; this role reads the factory token, so the allowlist stays exactly this subject."
  type        = list(string)
  default     = ["repo:Percona-Lab/jenkins-pipelines:ref:refs/heads/master"]
}
