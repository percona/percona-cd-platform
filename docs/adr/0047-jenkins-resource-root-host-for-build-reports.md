<!-- Copyright (C) 2026 Percona LLC -->
# 0047 - Jenkins resource root host for rendered build reports (ps80-assets)

**Status:** Accepted (2026-09-28)
**Related:** [ADR 0019](0019-shared-alb-ssl-termination-for-jenkins-masters.md) (the shared jenkins-masters ALB and its host Ingresses), [`docs/connectivity.md`](../connectivity.md) (Mode B web path).

## Context

- ps80 runs example jobs that build HTML reports with Claude on Bedrock. A single-file report with embedded JS and CSS renders broken when opened from the build's artifacts.
- Jenkins serves every archived build file on the main host with a sandbox Content-Security-Policy, read live on ps80: `sandbox allow-same-origin; default-src 'none'; img-src 'self'; style-src 'self';`. It blocks every script and every inline style.
- A resource root URL is Jenkins' supported answer: a second hostname that serves only user content. A request for an archived file on the main host is redirected to `<resource root>/<token>/...`, and Jenkins serves it there without the CSP header.
- The token encodes the requesting user and the path and is valid for 30 minutes (`ResourceDomainRootAction.validForMinutes`). An expired token redirects back to the main host, so the usual login and permissions apply again.
- Every other path on the resource host returns 404 (`ResourceDomainFilter`). `/robots.txt` is Jenkins' default `Disallow: /`.
- Jenkins recognises a resource request by comparing the configured host and port with the request's server name and port. ps80 already honours the forwarded headers the jenkins-ingress nginx sends: a Script Console request through the ALB reports `scheme=https`, `serverName=ps80.cd.percona.com` (from `X-Forwarded-Host`) and `serverPort=443`.

## Decision

- **A second host on the existing path.** `ps80-assets.cd.percona.com` is one more entry in `resources/addons/jenkins-ingress/values.yaml`, with the same upstream Service as ps80 and `upstreamHostHeader` set to the new name. The chart renders its Ingress in the `jenkins-masters` IngressGroup, external-dns publishes the record, and the ACM wildcard `*.cd.percona.com` covers it. No new AWS resources.
- **The setting is code.** `resources/jenkins-masters/ps80/init.groovy.d/resourceRootUrl.groovy` sets the URL at every boot through the ps80 init-config bucket, reads it back, and fails if Jenkins rejected it. An empty value clears it.
- **Storage does not move.** Reports stay ordinary build artifacts on ps80's data volume, governed by each job's build discarder.

## Alternatives considered

- **Relax the CSP** with the `hudson.model.DirectoryBrowserSupport.CSP` system property. Rejected: any build's archived HTML could then run script on the main host, in the session of whoever opens it.
- **Publish reports to S3.** Rejected for now: it needs a bucket, worker write access, a TLS front and an auth layer, and it moves report data out of Jenkins' permission model. Worth revisiting only if reports must outlive their builds or reach people without ps80 access.
- **Authentik in front of the resource host.** Rejected: the per-user token already limits access to users who can read the job, which is stricter than "any Percona SSO user", and a second login would sit on top of ps80's GitHub login.

## Rollout

- Host first: merge, let ArgoCD sync jenkins-ingress, and confirm `ps80-assets.cd.percona.com` resolves and reaches ps80.
- Setting second: `tofu apply` uploads the script to the ps80 init-config bucket, then `jenkins iac deploy --source s3` applies it without a restart.
- The reverse order breaks ps80: with the setting in place and no host, every artifact link redirects to a name that does not resolve.

## Consequences

- **(+)** Archived HTML reports on ps80 render with their own JS and CSS. A job prints one stable link, `${BUILD_URL}artifact/<path>`, which yields a fresh token after login.
- **(+)** The pattern repeats per master with one values entry and one init.groovy.d file.
- **(−)** A resource URL is a bearer URL for up to 30 minutes: anyone holding a copied link reads the file as the user who opened it. Links worth sharing are the main-host artifact URLs.
- **(−)** The resource host shares ps80's upstream, so it is down whenever ps80 is.
- **(−)** Every artifact request is redirected, API-token requests included, and Jenkins answers 400 when a request to the resource host carries credentials. HTTP clients must not forward them along the redirect: curl, Python `requests` and Rust `reqwest` drop them on a cross-host redirect, Python `urllib` forwards them (send auth with `add_unredirected_header`). Seen on the ci-ai-kit collector the day the setting landed.
