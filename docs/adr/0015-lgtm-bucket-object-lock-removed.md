# 0015: Remove S3 Object Lock default retention from LGTM buckets

**Status:** Accepted (2026-05-08)
**Amends:** [ADR 0011](0011-robustness-pass.md) decision H5 ("S3 Object
Lock (Compliance, 7 d) on LGTM buckets")
**Related:** [ADR 0010](0010-distributed-lgtm.md) (LGTM topology + S3
backends), [ADR 0014](0014-memberlist-cluster-label-isolation.md) (the
canary that surfaced the Loki write-path failure)

> **Clarification:** "remove" here means removing the bucket's **default retention rule**, not Object Lock itself. Object Lock cannot be disabled after bucket creation, so the buckets keep `ObjectLockEnabled: Enabled` (verified live), but with no retention rule and no legal hold it is dormant and does not gate writes. `terraform/lgtm-storage.tf` no longer declares `object_lock_enabled = true`, so consider re-adding it as a comment to reflect the immutable live state.

## Context

ADR 0011 H5 enabled S3 Object Lock with `COMPLIANCE` mode + 7 d default
retention on all three LGTM buckets
(`<cluster>-mimir-blocks`, `<cluster>-loki-chunks`,
`<cluster>-tempo-traces`). The intent was belt-and-braces anti-ransomware:
even a compromised Pod-Identity credential or a runaway compactor could not
delete recent observability data within the 7 d window.

Object Lock with default retention has a documented prerequisite at the
client side: every `PutObject` request must include either a `Content-MD5`
header or an `x-amz-checksum-*` header (CRC32, CRC32C, SHA1, SHA256, or
CRC64NVME). Without one of those, AWS rejects the request with:

```
InvalidRequest: Content-MD5 OR x-amz-checksum- HTTP header is required
for Put Object requests with Object Lock parameters
```

Loki's bundled S3 client uses AWS SDK Go **v1**, which does not emit
`Content-MD5` and does not auto-compute a `x-amz-checksum-*` header on
`PutObject` (auto-checksum is an SDK v2 feature, behind the `RequestChecksumCalculation`
config). Result during the ps3.cd canary, 2026-05-08:

1. Loki ingesters started up and tried to flush their first chunks.
2. Every PutObject was rejected with `InvalidRequest`. Ingester logs
   cycled `failed to flush user` / `level=error`.
3. Ingesters never marked themselves Ready (the readiness probe gates on
   "WAL replay complete + first flush succeeded").
4. The ingester ring never quorated. The distributor refused writes with
   `too few healthy ingesters`. The querier refused reads with the same.
5. The Loki read path was effectively dead end-to-end, despite the cluster,
   the bucket, the IAM, and the network all being correct.

Mimir was unaffected: it uses Thanos `objstore` with AWS SDK Go **v2**,
which emits `x-amz-checksum-crc32` on `PutObject` by default. Tempo was
unverified at the time but uses the same SDK v1 lineage as Loki for its S3
backend, so it was at imminent risk of the same failure mode.

The ADR 0011 H5 trade-off note ("Object Lock cannot be disabled once
enabled — re-creating these buckets later costs nothing since they're empty
at the time of the change") was correct on the easy direction but missed
the runtime-compatibility direction: even with Object Lock *enabled* on the
bucket, omitting the bucket-level **default retention** rule is sufficient
to allow PutObject without the checksum header. That's the configuration
this ADR adopts.

## Decision

**Remove the bucket-level default Object Lock retention** from all three
LGTM buckets. Keep the bucket-level `ObjectLockEnabled: Enabled` flag (it
is immutable on a created bucket and harmless without a default retention
rule). Lifecycle expiration handles the high-churn LGTM data shapes:

- Mimir blocks: 395 d (per ADR 0010 retention curve).
- Loki chunks: 30 d.
- Tempo traces: 14 d.

Object Lock COMPLIANCE was the wrong tool for these data shapes. LGTM data
is generated continuously by the components themselves; the threat model
that motivated H5 (a compromised credential mass-deleting historical data)
overlaps almost entirely with the threat model where the same credential
mass-overwrites the data with garbage. The 7 d window does not protect
against either case for data older than 7 d. Lifecycle-driven expiration
plus IAM scoping (Pod Identity bound to one bucket per component, no
cross-component access) is the upstream-recommended posture for
distributed LGTM and is what every reference deployment uses.

### Implementation

In `terraform/lgtm-storage.tf`, the entire
`aws_s3_bucket_object_lock_configuration "lgtm"` resource block is deleted.
The `aws_s3_bucket "lgtm"` resource keeps `object_lock_enabled = true`
(immutable; cannot be removed without bucket replacement). The bucket
versioning, KMS, public-access-block, and lifecycle resources are
unchanged.

All three buckets were empty at the time of the change (the canary was the
first push attempt that produced any objects), so no objects were stranded
under a retention they couldn't escape, and no migration was required.

Companion change in the same commit: `loki.loki.ingester.autoforget_unhealthy:
true` in `resources/addons/loki/values.yaml`. Auto-evict ring entries
whose heartbeat is past the timeout, so ungraceful pod terminations don't
pin UNHEALTHY entries in the ring forever. This is a separate concern from
Object Lock, but the canary surfaced both failure modes in the same hour
and the fix lands in the same change set.

Shipped in commit `7df2f57` of `Percona/percona-cd-platform`.

## Consequences

- **Loki write path verified end-to-end on 2026-05-08.** Ingester logs
  show `flushing stream` lines at the configured cadence. The ring is
  all-ACTIVE on three replicas. `{master="ps3.cd"}` queries through
  Grafana return Jenkins log lines from the master-side Alloy tail of
  `/var/log/jenkins/jenkins.log`.
- **Latent ransomware-defense gap.** Without bucket-level default
  retention, 24 h+ of write history is not protected from accidental or
  malicious delete. Lifecycle expiration is the only barrier, and
  expiration is delete-equivalent from the data-availability perspective.
  Acceptable: this is the upstream-recommended posture for distributed
  LGTM, and the Pod-Identity scoping (one component, one bucket, S3-only
  permissions on that bucket plus the LGTM CMK) blocks the most plausible
  accidental-delete vectors.
- **Per-object Object Lock still possible.** Because
  `ObjectLockEnabled: Enabled` is preserved at the bucket level, an
  individual object can still be PUT with explicit `x-amz-object-lock-*`
  headers if a future component wants per-write retention. None of the
  three LGTM components emit those headers today.
- **ADR 0011 H5 partially superseded.** The compactor + KMS + versioning
  +public-access-block hardening from H5 stays. The Object Lock default
  retention specifically is rolled back. ADR 0011 is otherwise unchanged.
- **Reversibility.** Re-enabling default retention is a one-resource
  Terraform addition. Re-enabling it once Loki has objects in the bucket
  is operationally identical to the original creation: PutObject without
  checksum will start failing again, ingesters will stall, and the read
  path will degrade. Any future re-enable should be gated on Loki shipping
  an SDK-v2-based S3 client (open upstream tracking issue,
  grafana/loki#11000-series) or on Loki gaining a config option to set
  `Content-MD5` explicitly.

## Tracking

- Implementation commit: `7df2f57` (`Percona/percona-cd-platform`).
- Buckets affected: `<cluster>-mimir-blocks`, `<cluster>-loki-chunks`,
  `<cluster>-tempo-traces` in `us-east-1`.
- Surfaced by: ps3.cd Alloy canary, 2026-05-08 (see
  [ADR 0013 amendments](0013-push-from-masters-with-nginx-bearer.md#amendments)
  and [ADR 0014](0014-memberlist-cluster-label-isolation.md) for the
  parallel memberlist-isolation finding from the same canary).
