# TLS strategy

One certificate, one termination layer, automatic renewal. Decision
records: [ADR 0003](adr/0003-acm-vs-cert-manager-for-alb.md) (ACM over
cert-manager for ALB termination),
[ADR 0007](adr/0007-cert-manager-deferred.md) (cert-manager deferred
outright), [ADR 0019](adr/0019-shared-alb-ssl-termination-for-jenkins-masters.md)
(shared-ALB termination for the Jenkins masters).

## The certificate

A single DNS-validated ACM wildcard for `*.cd.percona.com`
(`terraform/acm.tf`) covers every host the platform serves: the platform
UIs, the observability push endpoints, and all Jenkins master hostnames.
Validation records are auto-created in the hosted zone, and
`wait_for_validation` blocks the apply until the certificate is ISSUED, so
nothing downstream ever references an unverified ARN.

Renewal is ACM's own: DNS-validated certificates renew automatically for
as long as the validation records exist, which Terraform owns. Nothing
about the wildcard ever pages a human
([ACM managed renewal](https://docs.aws.amazon.com/acm/latest/userguide/managed-renewal.html)).

## Termination

TLS terminates at the two ALB groups and nowhere else in the cluster path:

- Every Ingress pins `ELBSecurityPolicy-TLS13-1-2-Res-PQ-2025-09`: TLS 1.3
  and 1.2 only, forward-secrecy ciphers, post-quantum hybrid key exchange
  (the policy AWS recommends since 2025-09). The pin appears in every
  addon's Ingress values and the Jenkins chart base values, because the
  AWS default policy still allows TLS 1.0/1.1.
- Listeners are HTTPS :443 with ssl-redirect. Certificates attach by SNI
  match against the wildcard, no per-Ingress certificate ARN is wired.
- Behind the ALB everything is deliberately plaintext: NGINX to the EC2
  masters on :8080 over VPC peering, pod traffic in-cluster, and the
  observability push path after its bearer check
  ([`connectivity.md`](connectivity.md)). The peering and security groups
  are the boundary, not transport encryption. There is no mTLS and none
  is planned while the in-cluster migration is the priority.

## Exceptions

- The certbot-fatality class of incident ended fleet-wide when the ALB took
  over TLS termination (pg, the last on-box TLS master, migrated 2026-07-07).
  No master terminates TLS on-box any longer.
- The internal signing and repo server manages its own transport and is
  out of this platform's scope.
