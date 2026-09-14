# Owner: platform
# Operator CIDR allowlists, resolved at plan time from SSM Parameter Store.
#
# The values are environment facts (operator egress IPs), not code. They are
# deliberately NOT aws_ssm_parameter resources: managing them here would put
# the values back into the public repo. Source of truth is the parameter
# value, amended with `just allowlist-set` (PutParameter is CloudTrail
# audited) followed by `just tf-plan` + `just tf-apply`. The resolved CIDRs
# land in the private S3-backed state via this read, like the resources they
# feed. That is accepted. Ops: docs/runbooks/eks-api-access.md.
# Decision: docs/adr/0036-operator-allowlists-in-ssm-and-dynamic-sso-access-entry.md.
#
# Parameters are StringList in the cluster region. Two tenants: the EKS API
# allowlist and the master break-glass :22 allowlist
# (/<cluster>/allowlist/master-ssh, consumed by every master-*.tf; docs/adr/0032
# keeps that path break-glass only). A third SSM tenant, the engineer SSH
# roster, is only NAMED here (local.master_ssh_engineer_roster at the bottom):
# the masters read it themselves, Terraform never does.
#
# The postcondition FAILS the plan (check blocks only warn in OpenTofu 1.11),
# keeping the hardening invariant fail-closed: the parameter must exist,
# parse to at least one valid CIDR, and never contain 0.0.0.0/0.

data "aws_ssm_parameter" "eks_api_allowlist" {
  name = "/${local.cluster_name}/allowlist/eks-api"

  lifecycle {
    postcondition {
      condition = alltrue([
        length(compact([for c in split(",", self.value) : trimspace(c)])) > 0,
        !contains([for c in split(",", self.value) : trimspace(c)], "0.0.0.0/0"),
        alltrue([for c in compact([for x in split(",", self.value) : trimspace(x)]) : can(cidrhost(c, 0))]),
      ])
      error_message = "The eks-api allowlist parameter must hold at least one valid CIDR and never 0.0.0.0/0 (docs/eks-hardening.md #2). Fix with `just allowlist-set eks-api <full,list>`."
    }
  }
}

data "aws_ssm_parameter" "master_ssh_allowlist" {
  name = "/${local.cluster_name}/allowlist/master-ssh"

  lifecycle {
    postcondition {
      condition = alltrue([
        length(compact([for c in split(",", self.value) : trimspace(c)])) > 0,
        !contains([for c in split(",", self.value) : trimspace(c)], "0.0.0.0/0"),
        alltrue([for c in compact([for x in split(",", self.value) : trimspace(x)]) : can(cidrhost(c, 0))]),
      ])
      error_message = "The master-ssh allowlist parameter must hold at least one valid CIDR and never 0.0.0.0/0 (docs/adr/0032, 0036). Fix with `just allowlist-set master-ssh <full,list>`."
    }
  }
}

locals {
  # aws_ssm_parameter values are sensitive-by-default; unwrapped like the
  # amis.tf consumers so the EKS endpoint plan diff stays readable.
  eks_api_allowed_cidrs = distinct(concat(
    compact([for c in split(",", nonsensitive(data.aws_ssm_parameter.eks_api_allowlist.value)) : trimspace(c)]),
    var.api_public_access_cidrs,
  ))

  # Break-glass :22 fleet allowlist, passed to every jenkins-master module.
  master_ssh_allowed_cidrs = compact([for c in split(",", nonsensitive(data.aws_ssm_parameter.master_ssh_allowlist.value)) : trimspace(c)])
}

# Engineer SSH roster (docs/adr/0046). Same write path as the allowlists
# (`just engineers-set`, PutParameter audited by CloudTrail), different read
# path: each master's engineer-keys SSM association reads the parameter on the
# host and reconciles /etc/ssh/authorized_keys.d, so the names enter neither
# the repo nor the state, and a write lands without a plan, an apply, or a
# rebuild. One fleet-wide list by design: an offboarding is one write.
locals {
  master_ssh_engineer_roster = {
    parameter_name   = "/${local.cluster_name}/access/master-ssh-engineers"
    parameter_region = data.aws_region.current.region
  }
}
