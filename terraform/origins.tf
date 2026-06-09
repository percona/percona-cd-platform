# Owner: platform
# Per-host "origin" Route 53 records: origin-<host>.cd.percona.com points at
# the existing EC2 Jenkins master's private IP, reached over the per-master
# VPC peering. The in-cluster NGINX proxy (resources/addons/jenkins-ingress)
# `proxy_pass`'es to this name, so the friendly `<host>.cd.percona.com` can
# flip from EC2 to the ALB without losing reachability to the underlying VM
# during cutover.
#
# Target resolution precedence:
#   1. `var.jenkins_origin_targets[<host>].target` — explicit operator
#      override in gitignored `local.auto.tfvars`. Rare; used to pin a
#      specific IP that overrides the discovery (debugging, parked master,
#      manual cutover).
#   2. `local.jenkins_discovered_origins[<host>]` — default. Live private IP
#      from `data.aws_instances.<host>_master` in `master-<host>.tf`, tag-
#      filtered by `iit-billing-tag=jenkins-<host>` and `instance_state_names
#      =["running"]`. Re-resolves on every `tofu apply` so SpotFleet
#      replacements, AZ failovers and renumbers self-heal automatically.
#
# Edge case: during a SpotFleet replacement window where no instance is in
# `running` state (~30-90s), both sources are null and apply will error.
# Acceptable failure mode: applies do not happen mid-rotation. If you need a
# safety net for unattended apply, set var.jenkins_origin_targets[<host>] to
# the last-known-good IP and it takes precedence.
#
# Mode A hosts (in-cluster StatefulSet, e.g. ps3-k8s) don't get an origin
# record — the ALB targets the K8s Service directly.

locals {
  # Per-master live IP discoveries. One line per master; each refers to the
  # tag-filtered `data.aws_instances.<host>_master` block declared in the
  # corresponding `master-<host>.tf`. The provider alias on the data source
  # is statically pinned to the master's region (TF provider aliases cannot
  # be dynamic), which is why this map enumerates hosts by name rather than
  # iterating var.jenkins_hosts.
  jenkins_discovered_origins = {
    # ps3 retired to the in-cluster jenkins-endpoint-reconciler (EndpointSlice); no origin record.
    # pxc = try(data.aws_instances.pxc_master.private_ips[0], null)  # when peered
  }

  jenkins_origin_records = {
    for h, c in var.jenkins_hosts : h => c
    if c.mode == "proxy" && (
      contains(keys(var.jenkins_origin_targets), h)
      || lookup(local.jenkins_discovered_origins, h, null) != null
    )
  }
}

resource "aws_route53_record" "origin" {
  for_each = local.jenkins_origin_records

  zone_id = local.route53_zone_id
  name    = "origin-${each.key}.${var.route53_zone_name}"
  type    = try(var.jenkins_origin_targets[each.key].type, "A")
  ttl     = 60
  records = [
    coalesce(
      try(var.jenkins_origin_targets[each.key].target, null),
      lookup(local.jenkins_discovered_origins, each.key, null),
    ),
  ]
}
