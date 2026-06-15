# Owner: platform
# Wildcard ACM cert for *.cd.percona.com.
# Single cert covers every host the platform serves through the shared ALB:
# argocd, grafana, the jenkins-proxy friendly names, ps3-k8s, and any
# future *.cd.percona.com endpoint. The cert only ever terminates at the
# ALB.
#
# DNS-validated via the existing public hosted zone. Validation records are
# auto-created in the same zone; wait_for_validation makes the apply block
# until the cert reaches ISSUED so downstream Helm releases that reference
# the ARN never observe an UNVERIFIED state.

module "acm" {
  source  = local.modules.acm.source
  version = local.modules.acm.version

  domain_name = "*.${var.route53_zone_name}"
  zone_id     = local.route53_zone_id

  validation_method      = "DNS"
  create_route53_records = true
  wait_for_validation    = true

  tags = local.tags
}
