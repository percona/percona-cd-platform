# Owner: platform
# Operator CIDR allowlists, resolved at plan time from SSM Parameter Store.
#
# The values are environment facts (operator egress IPs), not code. They are
# deliberately NOT aws_ssm_parameter resources: managing them here would put
# the values back into the public repo. Source of truth is the parameter
# value, amended with `just allowlist-set` (PutParameter is CloudTrail
# audited) followed by `just tf-plan` + `just tf-apply`. The resolved CIDRs
# land in the private S3-backed state via this read, like the resources they
# feed; that is accepted. Ops: docs/runbooks/operator-allowlists.md.
# Decision: docs/adr/0036-operator-allowlists-in-ssm-and-dynamic-sso-access-entry.md.
#
# Parameters are StringList in the cluster region. Today only the EKS API
# allowlist lives here; the master break-glass :22 allowlist
# (/<cluster>/allowlist/master-ssh) is the planned second tenant once its
# CIDR owners are confirmed (docs/adr/0032 keeps that path break-glass only).
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

locals {
  # aws_ssm_parameter values are sensitive-by-default; unwrapped like the
  # amis.tf consumers so the EKS endpoint plan diff stays readable.
  eks_api_allowed_cidrs = distinct(concat(
    compact([for c in split(",", nonsensitive(data.aws_ssm_parameter.eks_api_allowlist.value)) : trimspace(c)]),
    var.api_public_access_cidrs,
  ))
}
