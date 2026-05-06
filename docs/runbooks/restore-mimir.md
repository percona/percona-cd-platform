# Mimir block-restore runbook

Mimir's durable data lives in `s3://${cluster_name}-mimir-blocks` with
versioning enabled. This runbook covers the three plausible restore
scenarios.

## Scenario A — recovering a deleted block (operator error)

S3 versioning keeps non-current versions for 7 days. Use it.

```bash
aws s3api list-object-versions \
  --bucket percona-ci-platform-mimir-blocks \
  --prefix anonymous/01HK1234ABCD/  \
  --query 'DeleteMarkers[?IsLatest==`true`]'
```

Identify the delete-marker version IDs; remove them to bring the block
back:

```bash
aws s3api delete-object \
  --bucket percona-ci-platform-mimir-blocks \
  --key 'anonymous/01HK1234ABCD/meta.json' \
  --version-id <delete-marker-id>
```

Repeat for every key in the block (loop over the key list). Mimir will
re-discover the block on its next compactor cycle (~10 min).

## Scenario B — recovering from a runaway compactor

If the compactor wrote bad blocks (e.g. corrupt source-block references),
roll back compactor objects to the last-known-good versions:

```bash
# Identify the bad blocks via Mimir's `cortex_compactor_compactions_failed_total` metric
# Pick the timestamp where compactions started failing.

# For each affected block prefix, list versions newer than that timestamp:
aws s3api list-object-versions \
  --bucket percona-ci-platform-mimir-blocks \
  --prefix anonymous/<bad-block-id>/ \
  --query 'Versions[?LastModified > `2026-05-06T12:00:00Z`].[Key,VersionId]' \
  --output text | while read key vid; do
    aws s3api delete-object \
      --bucket percona-ci-platform-mimir-blocks \
      --key "$key" \
      --version-id "$vid"
done
```

Restart Mimir compactor + querier so caches are flushed:

```bash
kubectl -n mimir rollout restart deploy/mimir-compactor deploy/mimir-querier
```

## Scenario C — full bucket loss (AZ + replica failure)

Mimir's design assumes S3 is durable. If the bucket itself is gone (e.g.
ransomware, manual destruction, region failure), the plan is to replay
from Prometheus' WAL — which is bounded at 24 h with the agent-mode
config. Anything beyond 24 h is gone.

Mitigation roadmap (not yet implemented):

- **S3 cross-region replication** — `aws_s3_bucket_replication_configuration`
  in `terraform/lgtm-storage.tf`, replica bucket in `us-west-2` (or
  `eu-central-1` to align with the EU Jenkins masters).
- **S3 Object Lock** — turn on Compliance mode for a 7- or 14-day window
  to block ransomware-style mass-delete.

Both are tracked in `docs/eks-hardening.md` as follow-ups.

## Validation after any restore

```bash
# Querier sees the restored blocks:
kubectl -n mimir port-forward svc/mimir-querier 8080:8080 &
curl -H 'X-Scope-OrgID: percona-ci' \
  'http://localhost:8080/prometheus/api/v1/query?query=count(up)'

# Compactor catches up without errors:
kubectl -n mimir logs -l app.kubernetes.io/component=compactor --tail=200
```

Expected: query returns a non-empty response; compactor logs no `failed
to compact` errors over the next two compaction cycles.
