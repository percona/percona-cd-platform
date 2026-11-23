# 0051 - Alertmanager security mail through SES

**Status:** Accepted
**Related:** [ADR 0031](0031-in-cluster-synthetic-probing-for-jenkins-masters.md) (the Alertmanager had no receiver, a Slack receiver was the planned follow-up), [ADR 0050](0050-external-secrets-store-scoping.md) (the `mimir` namespace joins the store allowlist)

## Context

- The Mimir Alertmanager is the only alert router of the cluster. Its fallback config routes every alert to a receiver without an integration, so no alert reaches a person.
- Security alerts need a person now. A Slack receiver needs a bot token and a channel decision that do not exist yet.
- The account already sends mail through SES from `notify.cd.percona.com` (`terraform/ses-cve-agent.tf`), with DKIM, MAIL FROM and DMARC. The account is in the SES sandbox, and the verified `percona.com` identity covers `@percona.com` recipients.
- The Kubernetes and Mimir mixin alerts already fire into the Alertmanager. Mailing them would flood a personal mailbox.

## Decision

- Alertmanager mails alerts that carry the label `notify="security"` to one `@percona.com` mailbox. Every other alert stays on the empty receiver.
- The mail goes over the SES SMTP endpoint with STARTTLS, as `alerts@notify.cd.percona.com`. The IAM user `alertmanager-ses-smtp` may only call `ses:SendRawEmail` with that From address and `@percona.com` recipients.
- OpenTofu creates the user, its access key and one Secrets Manager secret with the SMTP login and the recipient (`terraform/ses-alertmanager.tf`). The recipient is a sensitive variable outside git.
- External Secrets reads that secret through the ClusterSecretStore `aws-secrets-manager-mimir`, which assumes an IAM role that may read only this one key ([ADR 0050](0050-external-secrets-store-scoping.md)). The namespace `mimir` is not on the list of the shared store.
- An ExternalSecret renders the complete Alertmanager config into a Secret. The route tree stays readable in git. The Secret replaces the chart's fallback ConfigMap.

## Consequences

- An alert rule opts in to mail with one label. A rule without it changes nothing.
- Mimir reads the fallback config only at start. A change of the route tree, the recipient or the SMTP login needs a restart of `mimir-alertmanager`.
- The SMTP password is in the OpenTofu state, like every generated key of this repo.
- Moving to Slack or a team mailbox later changes the receiver block and the secret, not the routing label.
- Operations: [`docs/runbooks/alertmanager-email.md`](../runbooks/alertmanager-email.md).
