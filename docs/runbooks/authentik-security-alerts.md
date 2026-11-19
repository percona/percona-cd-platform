# Authentik security alerts and gateway token lifetime

The Jenkins MCP gateway signs users in through Authentik. This page covers how long a gateway session lasts, the job that ends the sessions of users who stopped signing in, and the alerts on security signals at Authentik and at the gateway.

The Mimir Alertmanager has no receiver yet (ADR 0031). The alerts show in Alertmanager, in the Mimir ruler API and on the dashboards. They page nobody.

## Gateway token lifetime

The gateway client `jenkins-mcp-provider` (`resources/addons/authentik/templates/blueprint-jenkins-mcp.yaml`) issues access tokens for 30 minutes and refresh tokens for 7 days. Each refresh issues a new refresh token for another 7 days.

- A client that is used at least once in 7 days keeps working.
- A client that is idle for more than 7 days must sign in again through JumpCloud and the consent screen.
- A refresh token issued before the change keeps its old expiry. The last of them expires 30 days after the change.

To apply a change of the lifetime:

1. Merge the blueprint change and wait until ArgoCD shows the authentik Application Synced.
2. Restart the Authentik worker. The worker applies a changed blueprint only on boot (`authentik-blueprint-ops.md`, gotcha 2).
3. Read `refresh_token_validity` of `jenkins-mcp-provider` from `/api/v3/providers/oauth2/?name=jenkins-mcp-provider`. Expected result: `days=7`.

To roll back, set `refresh_token_validity: days=30` again and repeat steps 1 to 3.

## Offboarding job

The CronJob `authentik-gateway-token-offboarding` runs daily at 03:15 UTC. It deletes the gateway refresh tokens of every user whose last SSO login is older than 30 days. Service accounts are skipped. A user who left the company cannot sign in to JumpCloud again, so the gateway access ends. A user who still works signs in once and continues.

The job runs `ak shell` in the Authentik image with the database credentials the worker already has. It holds no API token. It deletes tokens and never marks them revoked, because a revoked token presented again fires `AuthentikRefreshTokenReplayed`.

The job is off by default. To turn it on:

1. Set `gatewayTokenOffboarding.enabled: "true"` and `gatewayTokenOffboarding.mode: report` in `resources/addons/authentik/values.yaml`.
2. Merge and wait for the next run, or start one with `kubectl -n authentik create job --from=cronjob/authentik-gateway-token-offboarding offboarding-manual-1`.
3. Read the job log. Expected result: one `gateway_token_offboarding_user` line per matched user and one `gateway_token_offboarding` summary line with `"deleted_rows": 0`.
4. Compare the matched users with the users you expect to lose access.
5. Set `mode: revoke` and merge.

If more users match than `maxUsers` (15), the job deletes nothing and fails. Read the summary line, then decide whether the match is right before you raise `maxUsers`.

To roll back, set `enabled: "false"` and merge. A deleted token cannot be restored. Its user signs in again.

## AuthentikRefreshTokenReplayed

Authentik refused a refresh token that it had already rotated or revoked. Two clients that share one token store and refresh at the same time cause this. A stolen token causes it too.

1. Open the Authentik admin interface, Events, and filter on the action `suspicious_request`.
2. Read the user, the client IP and the provider of the newest event.
3. Ask the user whether they run more than one MCP client or session on the same login.
4. If the user does not know the client IP, delete that user's refresh tokens for the provider and tell the user to sign in again.

## AuthentikSourceMappingFailed

A user or group mapping on the JumpCloud SAML source raised an error, so Authentik refused the login. An assertion with an XML comment in the NameID or an attribute value causes this. That is an account takeover attempt. A broken mapping causes it too, and then every login fails.

1. Open the Authentik admin interface, Events, and filter on the action `configuration_error`.
2. Read the mapping name in the event message.
3. If the mapping is `saml-xml-comment-guard`, find the user of the attempt in the matching `property_mapping_exception` event and report it to security.
4. If the failures repeat for several users, read the mapping expression in `blueprint-grafana.yaml` and fix it. Logins are down until then.

## JenkinsMcpAuthzDenialBurst

The gateway denied more than 4 tool calls in 15 minutes. Normal traffic has about one denial in five days.

1. Run this LogQL in Grafana Explore on the Loki data source: `{instance=~"jenkins-mcp/.+"} |= "mcp_tool_call" | json | status="denied"`.
2. Read `preferred_username`, `tool`, `master` and `deny_reason` of the lines.
3. If one user calls an operate tool with `not_writer`, ask whether they need the writers group.
4. If one user tries many tools or masters, treat it as a probe and ask the user.

## JenkinsMcpUnauthorizedBurst

The rate of HTTP 401 answers on `/mcp` is above 0.2 per second and 10 times the rate of the week before, for 15 minutes.

1. Open the jenkins-mcp dashboard and read the panel "HTTP responses on /mcp".
2. If the 200 rate dropped to 0, real users are rejected. Check the Authentik server and its signing key first.
3. If the 200 rate is normal, the 401 answers come from clients without a valid token. Check that the gateway pods keep up (CPU and latency panels).
4. If the burst lasts or slows the gateway, discuss a rate limit or WAF rule on the shared ALB with the platform owner.

## Thresholds

The gateway thresholds are first guesses from about 12,000 tool calls and 6 denials in 29 days. After one week of data, compare each alert with the dashboards and adjust the numbers in `resources/addons/jenkins-mcp/templates/prometheusrule.yaml`.
