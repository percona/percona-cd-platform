# Per-host "origin" Route 53 records: origin-<host>.cd.percona.com points at
# the existing EC2 Jenkins master's public DNS or IP. The in-cluster NGINX
# proxy (Mode B in resources/jenkins/proxy/) `proxy_pass`'es to this name, so
# the friendly `<host>.cd.percona.com` can flip from EC2 to the ALB without
# losing reachability to the underlying VM during cutover.
#
# Targets are operationally sensitive (per-master, multi-region), so they
# live in `terraform/local.auto.tfvars` (gitignored), not in this public
# repo. Until populated, no origin records are created — operators roll
# them out one host at a time as each Jenkins master is wired into the
# proxy data path.
#
# Mode A hosts (in-cluster StatefulSet, e.g. ps3-k8s) don't get an origin
# record — the ALB targets the K8s Service directly.

locals {
  jenkins_origin_records = {
    for h, c in var.jenkins_hosts : h => c
    if c.mode == "proxy" && contains(keys(var.jenkins_origin_targets), h)
  }
}

resource "aws_route53_record" "origin" {
  for_each = local.jenkins_origin_records

  zone_id = local.route53_zone_id
  name    = "origin-${each.key}.${var.route53_zone_name}"
  type    = var.jenkins_origin_targets[each.key].type
  ttl     = 60
  records = [var.jenkins_origin_targets[each.key].target]
}
