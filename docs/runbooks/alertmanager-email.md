# Alertmanager security mail

The Mimir Alertmanager mails alerts with the label `notify="security"` to one
`@percona.com` mailbox. All other alerts go to the empty receiver. Design:
[ADR 0051](../adr/0051-alertmanager-security-mail-through-ses.md).

| Piece | Where | Owner |
|---|---|---|
| IAM user, access key, SES send policy | `terraform/ses-alertmanager.tf` | OpenTofu |
| SMTP login and recipient | Secrets Manager `percona-ci-platform/mimir/alertmanager-smtp`, JSON `smtp_username`, `smtp_password`, `email_to` | OpenTofu, write-only version |
| Recipient value | `alertmanager_email_to` in the gitignored `terraform/local.auto.tfvars` | Operator |
| Route tree and receivers | `resources/addons/mimir/files/alertmanager-fallback.yaml` | GitOps |
| Rendered config | Secret `mimir/mimir-alertmanager-config`, from the ExternalSecret of the same name | ESO |
| Mount | `/configs/alertmanager_fallback_config.yaml` in `mimir-alertmanager` | mimir addon values |

## How the mail path works

1. A PrometheusRule alert with `notify: security` fires in the Mimir ruler.
2. The ruler sends it to `mimir-alertmanager`, tenant `anonymous`.
3. The tenant has no stored config, so Mimir uses the fallback config file.
4. The route waits 1 minute, groups by `alertname` and `namespace`, and repeats after 12 hours.
5. Alertmanager sends the mail to `email-smtp.us-east-1.amazonaws.com:587` with STARTTLS and SMTP AUTH.
6. SES delivers it from `alerts@notify.cd.percona.com`. A resolved mail follows when the alert ends.

Mimir reads the fallback file only at start. ESO updates the Secret within 1 hour,
but the running Alertmanager keeps the old config until a restart.

## Change the recipient

1. Set the new address in `terraform/local.auto.tfvars`.

   ```hcl
   alertmanager_email_to = "someone@percona.com"
   ```

2. Increment `secret_string_wo_version` in `terraform/ses-alertmanager.tf` and commit it.
   Without the bump the provider keeps the old value in Secrets Manager.
3. Plan and apply. The plan changes only the secret version.

   ```bash
   export AWS_PROFILE=percona-dev-admin
   just tf-plan
   just tf-apply
   ```

4. Force an ESO refresh.

   ```bash
   kubectl -n mimir annotate externalsecret mimir-alertmanager-config force-sync="$(date +%s)" --overwrite
   ```

5. Restart the Alertmanager.

   ```bash
   kubectl -n mimir rollout restart statefulset mimir-alertmanager
   kubectl -n mimir rollout status statefulset mimir-alertmanager
   ```

6. Send a test alert (next section).

## Rotate the SMTP login

1. Replace the key. `taint` is a raw-tofu exception flagged by this runbook.

   ```bash
   tofu -chdir=terraform taint aws_iam_access_key.alertmanager_ses_smtp
   ```

2. Run steps 2 to 6 of "Change the recipient". Mail fails between the apply and the restart.

## Send a test alert

1. Open a port to the Alertmanager.

   ```bash
   kubectl -n mimir port-forward svc/mimir-alertmanager 8080:8080
   ```

2. Post an alert with the label in a second shell.

   ```bash
   curl -sS -X POST -H 'Content-Type: application/json' http://localhost:8080/alertmanager/api/v2/alerts \
     -d '[{"labels":{"alertname":"AlertmanagerMailTest","notify":"security","namespace":"mimir","severity":"info"},
           "annotations":{"summary":"Test of the Alertmanager security mail"}}]'
   ```

3. Expect a `[FIRING:1] AlertmanagerMailTest` mail after about 1 minute.
4. Expect the resolved mail about 10 minutes after the first one. The alert times out after 5 minutes and the
   next group interval sends the resolved mail.

## No mail arrives

1. Check that the alert reached the Alertmanager.

   ```bash
   curl -sS http://localhost:8080/alertmanager/api/v2/alerts | jq '.[].labels'
   ```

2. Check that the running config has the receiver `security-email`.

   ```bash
   curl -sS http://localhost:8080/alertmanager/api/v2/status | jq -r .config.original | grep -n security-email
   ```

   If it is missing, the pod started before the Secret changed. Restart it (step 5 of "Change the recipient").
3. Check that the Secret synced.

   ```bash
   kubectl -n mimir get externalsecret mimir-alertmanager-config
   ```

   Expect `SecretSynced` and `READY True`. The ExternalSecret uses the ClusterSecretStore
   `aws-secrets-manager-mimir`, which assumes the IAM role `percona-ci-platform-eso-mimir`
   (`scopedStores.mimir` in `resources/addons/external-secrets/values.yaml`). On a store error,
   read `kubectl describe clustersecretstore aws-secrets-manager-mimir`.
4. Read the notify errors.

   ```bash
   kubectl -n mimir logs statefulset/mimir-alertmanager | grep -i -E 'notify|email|smtp'
   ```

   - `535 Authentication Credentials Invalid`: the SMTP password is not for `us-east-1`, or the key was rotated without a restart.
   - `554 Message rejected: Email address is not verified`: the recipient is not `@percona.com`, or the SES identity lost its verification.
   - `not authorized to perform ses:SendRawEmail`: the From address or the recipient fails the IAM conditions.
   - `invalid service state: Terminated, expected: Running`: the alertmanager ring replication factor is 1. Remove the override. The default 3 works with one replica.
5. Check the SES send counters for the last hour.

   ```bash
   aws ses get-send-statistics --region us-east-1 | jq '.SendDataPoints | sort_by(.Timestamp) | .[-4:]'
   ```

6. Check the spam folder of the mailbox. DMARC on `notify.cd.percona.com` is `p=quarantine`.
