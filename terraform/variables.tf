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
    in extended support incurs the paid extended-support fee (CLAUDE.md rule).
    Verify with `aws eks describe-cluster-versions`.
  EOT
  type        = string
  default     = "1.35"
}

variable "vpc_cidr" {
  description = "Cluster VPC CIDR. Avoid the Jenkins-VPC ranges 10.144/.155/.166/.177/.179/.188/.199 (multi-region masters)."
  type        = string
  default     = "10.220.0.0/16"
}

variable "argocd_hostname" {
  description = "Public hostname for ArgoCD UI. external-dns publishes the ALB alias."
  type        = string
  default     = "argocd.cd.percona.com"
}

variable "grafana_hostname" {
  description = "Public hostname for Grafana UI."
  type        = string
  default     = "grafana.cd.percona.com"
}

variable "route53_zone_name" {
  description = "Public hosted zone for *.cd.percona.com (Z1H0AFAU7N8IMC)."
  type        = string
  default     = "cd.percona.com"
}

variable "access_entries" {
  description = <<-EOT
    EKS access entries. Map keyed by entry name -> principal ARN + AWS-managed
    access policies. Replaces the legacy aws-auth ConfigMap.

    Operators populate this in `terraform/local.auto.tfvars` (gitignored) so
    the public repo never carries IAM ARNs. Example:

    access_entries = {
      anderson = {
        principal_arn = "arn:aws:iam::<account>:user/anderson.nogueira"
        policy_associations = {
          admin = {
            policy_arn   = "arn:aws:iam::aws:policy/AmazonEKSClusterAdminPolicy"
            access_scope = { type = "cluster" }
          }
        }
      }
    }

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
    Allowlist for the EKS public API endpoint. No default — operators MUST set
    this in `terraform/local.auto.tfvars` (gitignored) before applying. Public-
    unrestricted (0.0.0.0/0) defeats the hardening baseline; use the Percona
    office / VPN CIDRs and any operator-laptop egress IPs.

    docs/eks-hardening.md item #2.
  EOT
  type        = list(string)

  validation {
    condition     = length(var.api_public_access_cidrs) > 0 && !contains(var.api_public_access_cidrs, "0.0.0.0/0")
    error_message = "Set at least one CIDR; 0.0.0.0/0 is forbidden by the hardening baseline (docs/eks-hardening.md #2)."
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

variable "jenkins_hosts" {
  description = <<-EOT
    Jenkins masters governed by this platform. mode = in-cluster (StatefulSet) or
    proxy (NGINX Deployment reverse-proxying to a per-host origin EC2 master).
    Add a host's mode = in-cluster after migration; before, keep it as proxy.
  EOT
  type = map(object({
    mode             = string # "in-cluster" or "proxy"
    upstream_origin  = optional(string)
    upstream_az      = optional(string)
    storage_size_gib = optional(number)
  }))
  default = {
    # 9 EC2 masters → Mode B (ALB → in-cluster NGINX → EC2 origin). The
    # friendly `<host>.cd.percona.com` flips from EC2 to the ALB;
    # `origin-<host>.cd.percona.com` keeps pointing at the EC2 master so
    # the proxy upstream stays reachable through cutover.
    pmm   = { mode = "proxy", upstream_origin = "origin-pmm.cd.percona.com" }
    ps80  = { mode = "proxy", upstream_origin = "origin-ps80.cd.percona.com" }
    pxc   = { mode = "proxy", upstream_origin = "origin-pxc.cd.percona.com" }
    pxb   = { mode = "proxy", upstream_origin = "origin-pxb.cd.percona.com" }
    psmdb = { mode = "proxy", upstream_origin = "origin-psmdb.cd.percona.com" }
    pg    = { mode = "proxy", upstream_origin = "origin-pg.cd.percona.com" }
    ps57  = { mode = "proxy", upstream_origin = "origin-ps57.cd.percona.com" }
    rel   = { mode = "proxy", upstream_origin = "origin-rel.cd.percona.com" }
    cloud = { mode = "proxy", upstream_origin = "origin-cloud.cd.percona.com" }

    # ps3-k8s = first in-cluster Jenkins master. Seeded as a full replica of
    # the production EC2 ps3 via cross-region EBS snapshot copy of
    # JENKINS_HOME (see runbooks/migrate-ps3-to-eks.md). Runs in parallel
    # with EC2 ps3 for validation; cutover is a DNS flip of
    # ps3.cd.percona.com to this host.
    #
    # Production ps3.cd.percona.com is intentionally NOT in this map — it
    # stays on its current EC2 path, fully outside platform scope, until the
    # cutover. Add a `ps3` entry here only when the EC2 master is retired.
    "ps3-k8s" = {
      mode             = "in-cluster"
      upstream_az      = "us-east-1a"
      storage_size_gib = 100
    }
  }
}

variable "jenkins_origin_targets" {
  description = <<-EOT
    Per-host origin record values for Mode B (proxy) Jenkins masters.
    Keyed by short host name (matches a key in var.jenkins_hosts where
    mode = "proxy"). Each entry is the existing EC2 master's reachable
    public hostname or IP. The in-cluster NGINX proxy `proxy_pass`'es to
    `origin-<host>.cd.percona.com`, which CNAME/A-records to `target` here.

    Operationally sensitive (per-master, multi-region). Populate via
    `terraform/local.auto.tfvars` (gitignored) — never commit. Hosts not
    listed here get no origin record (intentional: roll out one at a time).

    Example (in local.auto.tfvars):
      jenkins_origin_targets = {
        pmm  = { target = "ec2-X-Y-Z.compute-1.amazonaws.com", type = "CNAME" }
        ps80 = { target = "203.0.113.10",                       type = "A"     }
      }
  EOT
  type = map(object({
    target = string
    type   = optional(string, "CNAME")
  }))
  default = {}
  # Not marked sensitive: target values are public DNS/IPs and `for_each`
  # over sensitive values is forbidden. Privacy is enforced by keeping
  # `terraform/local.auto.tfvars` out of git, not by the type system.
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
    front-doors Grafana via OIDC). See docs/adr/0012-authentik-bridge.md
    (forthcoming) for the architecture rationale.
  EOT
  type        = bool
  default     = false
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
    the percona-dev-admin account's cleanup automation — do not drop them:

    - `iit-billing-tag` — IaC/LambdaEC2Cleanup.yml terminates EC2 instances
      missing this tag (any value) after 10 minutes.
    - `PerconaKeep` — IaC/LambdaVolumeCleanup.yml deletes any `available`
      EBS volume daily unless this tag is present (capital P, capital K).

    EBS volumes provisioned by the in-cluster aws-ebs-csi-driver pick up
    these tags via StorageClass `parameters.tagSpecification_*` (see
    resources/addons/storageclass-gp3/templates/storageclasses.yaml).
  EOT
  type        = map(string)
  default = {
    "iit-billing-tag" = "percona-ci-platform"
    "PerconaKeep"     = "True"
    "managed-by"      = "opentofu"
    "repo"            = "github.com/nogueiraanderson/percona-ci-platform"
  }
}
