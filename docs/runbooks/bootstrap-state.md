# Bootstrap the OpenTofu state backend

The S3 bucket that holds this repo's tofu state is chicken-and-egg infra:
it can't live in the same state file it backs. Pre-created, one-time, by
the runbook below. State locking uses native S3 conditional writes
(`use_lockfile = true` in `terraform/backend.tf`) — a sibling `.tflock`
object next to the state file.

**Already done (2026-04-30):** `s3://terraform-state-storage-percona-ci-platform`
exists in the `percona-dev-admin` account, `us-east-1`.

## Recreate

```bash
export AWS_PROFILE=<your-profile>

# S3 bucket — versioned, SSE-S3, public-access-block locked.
aws s3 mb s3://terraform-state-storage-percona-ci-platform --region us-east-1

aws s3api put-bucket-versioning \
  --bucket terraform-state-storage-percona-ci-platform \
  --versioning-configuration Status=Enabled

aws s3api put-bucket-encryption \
  --bucket terraform-state-storage-percona-ci-platform \
  --server-side-encryption-configuration \
  '{"Rules":[{"ApplyServerSideEncryptionByDefault":{"SSEAlgorithm":"AES256"}}]}'

aws s3api put-public-access-block \
  --bucket terraform-state-storage-percona-ci-platform \
  --public-access-block-configuration \
  BlockPublicAcls=true,IgnorePublicAcls=true,BlockPublicPolicy=true,RestrictPublicBuckets=true
```

## Verify

```bash
aws s3api get-bucket-versioning --bucket terraform-state-storage-percona-ci-platform
aws s3api get-public-access-block --bucket terraform-state-storage-percona-ci-platform
```

## Why this isn't in tofu

Bootstrapping the state backend with the same tool that depends on it
creates a circular dependency. The standard pattern is to keep this
runbook as the manual step, document the inputs (region, names), and
rely on AWS-side immutability of the bucket/table going forward.
