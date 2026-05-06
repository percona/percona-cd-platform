locals {
  cluster_name = var.cluster_name
  region       = var.aws_region

  # Inherits var.tags via provider default_tags. Modules that take their own
  # `tags` argument should pass `local.tags` so the merged set is consistent.
  tags = var.tags

  # Derived hostnames for external-dns + ALB Ingresses (no hardcoded host strings elsewhere).
  jenkins_friendly_hosts = [for h, _ in var.jenkins_hosts : "${h}.${var.route53_zone_name}"]
  jenkins_origin_hosts   = [for h, c in var.jenkins_hosts : c.upstream_origin if try(c.upstream_origin, "") != ""]

  # State bucket name — single source of truth referenced by docs/runbooks/.
  # Native S3 locking (use_lockfile in backend.tf) writes a sibling .tflock
  # object next to the state file; no separate lock service needed.
  state_bucket = "terraform-state-storage-${local.cluster_name}"

  # Resolved Route 53 ID for the platform zone. Cached locally so all consumers
  # (pod-identity, acm, origins, argocd) reference the same value.
  route53_zone_id = data.aws_route53_zone.main.zone_id

  # NodeGroup sizing knobs surfaced as locals so they're easy to find.
  ng = {
    system = {
      instance_types = ["t3.medium"]
      min_size       = 2
      desired_size   = 2
      max_size       = 3
    }
    prometheus_system = {
      instance_types = ["m6a.large"]
      min_size       = 1
      desired_size   = 1
      max_size       = 2
    }
    jenkins_system = {
      instance_types = ["m6a.xlarge"]
      min_size       = 1
      desired_size   = 1
      max_size       = 5
    }
  }
}
