# Shared data sources. Keep narrow — single-line lookups only; complex
# computed values belong in locals.tf.

data "aws_caller_identity" "current" {}
data "aws_region" "current" {}

# Public hosted zone for *.cd.percona.com. ACM, external-dns, and the per-host
# `origin-<host>` records all hang off this lookup.
data "aws_route53_zone" "main" {
  name         = var.route53_zone_name
  private_zone = false
}
