# ps3 AWS_STASH credential

`AWS_STASH` on the in-cluster ps3 controller is the access key of the IAM user
`jenkins-artifactory-ps3` (`terraform/iam-jenkins-artifactory-ps3.tf`). The shared
`jenkins-artifactory` policy scopes it to the `percona-jenkins-artifactory` and
`percona-jenkins-artifactory-public` buckets. The production masters keep their own
shared user, so a ps3 rotation or revocation touches nothing else.

| Piece | Where | Owner |
|---|---|---|
| IAM user, access key | `aws_iam_user.jenkins_artifactory_ps3`, `aws_iam_access_key.jenkins_artifactory_ps3` | OpenTofu |
| Key pair, source of truth | Secrets Manager `percona-ci-platform/jenkins/ps3/aws-stash`, JSON `access_key_id` and `secret_access_key` | OpenTofu, write-only version |
| Read grant | `SecretsManagerReadPs3` on the ps3 controller role (`terraform/iam-jenkins-controller.tf`) | OpenTofu |
| Jenkins credential | `AWS_STASH`, global domain of the system store, type AWS credentials | `scripts/ps3-aws-stash-reload.groovy` |

The Jenkins side is a Script Console step, not GitOps, for two reasons. The Secrets
Manager credentials provider on ps3 builds only string, username-password, SSH key,
certificate and file credentials, so the secret carries no `jenkins:credentials:type`
tag and the provider skips it. A JCasC `credentials` section replaces the whole system
store on every reload, so declaring `AWS_STASH` there waits for the migration of the
remaining system-store credentials. The reload script reads the secret inside the pod
through Pod Identity and prints only the access key id.

## Rotate

Jobs that bind `AWS_STASH` fail between the apply and the reload, so run the two
steps back to back.

1. Replace the key and bump the secret version. `taint` is a raw-tofu exception
   flagged by this runbook, there is no `just` recipe for it.

   ```bash
   export AWS_PROFILE=percona-dev-admin
   tofu -chdir=terraform taint aws_iam_access_key.jenkins_artifactory_ps3
   ```

   Then increment `secret_string_wo_version` in
   `terraform/iam-jenkins-artifactory-ps3.tf` and commit it. Without the bump the
   provider keeps the old key pair in Secrets Manager.

2. Plan and apply. The plan shows the access key as 1 to add and 1 to destroy, the
   secret version as an in-place change, and nothing else from this file.

   ```bash
   just tf-plan
   just tf-apply
   ```

3. Reload the credential on ps3.

   ```bash
   jenkins admin -i ps3 groovy -f scripts/ps3-aws-stash-reload.groovy
   ```

   The output is `AWS_STASH updated, access key id AKIA...`. Compare it with
   `aws iam list-access-keys --user-name jenkins-artifactory-ps3`, which lists exactly
   one active key with that id.

4. Verify with a throwaway job on ps3 (`agent { label 'docker' }`, the pipeline binds
   `AWS_STASH` with `AmazonWebServicesCredentialsBinding` and runs
   `aws s3 ls s3://percona-jenkins-artifactory/ | head -n 1`). Delete the job
   afterwards and confirm it is gone from `jenkins -i ps3 job list`.

## Revoke after a leak

Step 1 above deletes the leaked key at the next apply, so run steps 1 to 3 at once.
For an immediate cut before the apply, deactivate the key by hand and let the apply
replace it:

```bash
aws iam update-access-key --user-name jenkins-artifactory-ps3 --access-key-id <id> --status Inactive
```

## First install

The first apply creates the user, the key and the secret. Step 3 then replaces the
`AWS_STASH` entry already present in the ps3 system store (the script updates an
existing credential in place and creates it when absent), and step 4 proves it.
