# Non-blocking platform invariants. `check` blocks emit warnings at plan time
# and never block apply -- the right place for "we documented this, but
# operators forget" gates.

# Addon-pin guardrail. eks-addons.tf header says "deliberately unset on first
# apply, pin via var.addon_versions after". This check turns that documented
# step into a `tofu plan` warning instead of a doc paragraph that gets missed.
#
# The scoped data source gates evaluation: on the very first apply (cluster
# doesn't exist yet), the lookup fails and OpenTofu skips the assert with a
# benign "check could not evaluate" notice. After the first apply, the data
# source resolves and the assert fires whenever var.addon_versions is empty.
check "addon_versions_pinned" {
  data "aws_eks_cluster" "this" {
    name = local.cluster_name
  }

  assert {
    condition     = length(var.addon_versions) > 0
    error_message = "var.addon_versions is empty -- EKS resolves to channel-default versions per var.cluster_version. Pin each managed addon in terraform/local.auto.tfvars (docs/eks-hardening.md #5). Look up current channel defaults with `aws eks describe-addon-versions --kubernetes-version ${var.cluster_version} --addon-name <name>`."
  }
}
