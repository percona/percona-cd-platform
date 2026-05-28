# Sweeping orphan LGTM PVCs

Use this runbook when `kubectl get pvc -n {mimir,loki,tempo}` shows PVCs
without an owning Pod, or when AWS billing surfaces idle gp3 volumes
tagged for the cluster. The 2026-05-13 zone-aware -> single-replica
collapse left 17 orphan PVCs (~445 GiB EBS); this is the procedure that
cleared them.

After [ADR 0020](../adr/0020-lgtm-single-az-collapse.md) added
`persistentVolumeClaimRetentionPolicy: {whenDeleted: Delete, whenScaled: Delete}`
to every stateful component, **routine scale-downs and topology flips
self-clean**. This runbook covers legacy orphans (one-shot cleanup) and
the chart-default Pending PVC class (e.g. `kafka-data-mimir-kafka-0`
even though `kafka.enabled: false`).

## When to run

- `kubectl get pvc -A` shows a PVC bound but no Pod with the matching
  STS ordinal exists.
- `kubectl get pv | awk '$5=="Released"'` shows a PV stuck in Released
  on the `gp3-monitoring-1a-retain` class (Retain reclaim blocks the
  cleanup Lambda).
- Mimir/Loki/Tempo Helm value flip left zone-aware StatefulSets pruned
  but their PVCs intact.

## What stays, what goes

The canonical stateful set after ADR 0020:

```
mimir/storage-mimir-ingester-0           50 GiB
mimir/storage-mimir-store-gateway-0      30 GiB
mimir/storage-mimir-compactor-0          30 GiB
mimir/storage-mimir-alertmanager-0       5 GiB  (single-replica per ADR 0020)
loki/data-loki-ingester-0                50 GiB
loki/data-loki-compactor-0               30 GiB
tempo/data-tempo-ingester-0              30 GiB
```

Anything else under `mimir/`, `loki/`, `tempo/` that does not have a
running Pod is a sweep candidate. Specifically:

- `storage-mimir-alertmanager-{1,2}` (legacy 3-replica zone-aware AM)
- `*-zone-{a,b,c}-*` PVCs (legacy zone-aware StatefulSets)
- `data-tempo-ingester-{1,2}` (legacy 3-replica Tempo)
- `kafka-data-mimir-kafka-0` (chart-default Kafka StatefulSet when
  `kafka.enabled: false` is the active config)
- Any `*-prepare-shutdown-*` or `*-flush-*` leftover from a manual drain

For the active canonical PVC migration (relocating bound PVCs to a
different AZ, e.g. the ADR 0023 sweep), use [`lgtm-az-migration.md`](lgtm-az-migration.md)
instead. This runbook only covers orphans where the bound PVC has no
owning Pod.

## Pre-flight (read-only)

```sh
# Canonical pods Ready
kubectl -n mimir get pod -l name=ingester
kubectl -n loki  get pod -l app.kubernetes.io/component=ingester
kubectl -n tempo get pod -l app.kubernetes.io/component=ingester

# Ring has no LEAVING/PENDING members
kubectl -n mimir port-forward svc/mimir-distributor 8080:8080 &
PF=$!
curl -s http://localhost:8080/ingester/ring | grep -E 'LEAVING|PENDING'   # must be empty
kill $PF

# Ingest still flowing from all 10 masters
./scripts/check-master-ingest.sh

# End-to-end pipeline
./scripts/verify-observability.sh --skip-master

# Snapshot the orphan inventory (save for audit trail)
kubectl get pvc -A -o wide > /tmp/pvc-before-sweep.txt
kubectl get pv  -o wide > /tmp/pv-before-sweep.txt
```

Abort if any check fails. The sweep relies on the WAL being flushed and
the canonical ingesters being the authoritative ring members.

## Sweep PVCs (gp3 Delete reclaim auto-cleans EBS)

```sh
# Mimir
kubectl -n mimir delete pvc \
  storage-mimir-ingester-zone-a-0 \
  storage-mimir-ingester-zone-b-0 \
  storage-mimir-ingester-zone-c-0 \
  storage-mimir-store-gateway-zone-a-0 \
  storage-mimir-store-gateway-zone-b-0 \
  storage-mimir-store-gateway-zone-c-0 \
  storage-mimir-alertmanager-zone-a-0 \
  storage-mimir-alertmanager-zone-b-0 \
  storage-mimir-alertmanager-zone-c-0 \
  kafka-data-mimir-kafka-0

# Loki
kubectl -n loki delete pvc \
  data-loki-ingester-zone-a-0 \
  data-loki-ingester-zone-b-0 \
  data-loki-ingester-zone-c-0

# Tempo
kubectl -n tempo delete pvc \
  data-tempo-ingester-1 \
  data-tempo-ingester-2 \
  data-tempo-ingester-zone-a-0 \
  data-tempo-ingester-zone-b-0 \
  data-tempo-ingester-zone-c-0
```

Adjust the list to match your inventory. PVC delete on a `gp3` SC PV
triggers EBS volume delete via the CSI driver; no AWS-side action
needed.

## Sweep Released PVs on retain SCs (manual EBS delete)

The `gp3-monitoring-1a-retain` and `gp3-jenkins-1a-retain` StorageClasses
use `reclaimPolicy: Retain`. They also tag EBS volumes with
`PerconaKeep=True` via `tagSpecification_*`, which **blocks the
percona-dev-admin cleanup Lambda**. Released PVs on these SCs need an
explicit `aws ec2 delete-volume`.

```sh
# For each Released PV
for PV in $(kubectl get pv -o jsonpath='{range .items[?(@.status.phase=="Released")]}{.metadata.name}{"\n"}{end}'); do
  VOL=$(kubectl get pv $PV -o jsonpath='{.spec.csi.volumeHandle}')
  SC=$(kubectl get pv $PV -o jsonpath='{.spec.storageClassName}')
  echo "PV=$PV SC=$SC VOL=$VOL"
  # Inspect before deleting
done

# Then for each confirmed-safe PV:
PV=pvc-xxxxxxxx-xxxx-xxxx-xxxx-xxxxxxxxxxxx
VOL=$(kubectl get pv $PV -o jsonpath='{.spec.csi.volumeHandle}')
kubectl delete pv $PV
aws ec2 delete-volume --volume-id $VOL --profile percona-dev-admin --region us-east-1
```

## Verification

```sh
# Orphan PVCs gone
kubectl get pvc -A | grep -E 'zone-[abc]|tempo-ingester-[12]|kafka-data-mimir'   # empty

# Released PVs gone
kubectl get pv | awk '$5=="Released"'                                            # empty

# LGTM pods still Running, 0 restarts
kubectl get pods -n mimir; kubectl get pods -n loki; kubectl get pods -n tempo

# Ingest still flowing
./scripts/check-master-ingest.sh

# AWS EBS volume count dropped
aws ec2 describe-volumes --profile percona-dev-admin --region us-east-1 \
  --filters "Name=tag:kubernetes.io/cluster/percona-ci-platform,Values=owned" \
  --query 'length(Volumes)'

# ArgoCD apps stay Synced + Healthy
kubectl get applications -n argocd -o custom-columns=NAME:.metadata.name,SYNC:.status.sync.status,HEALTH:.status.health.status \
  | grep -E 'mimir|loki|tempo|grafana'
```

## Rollback

EBS deletion is irreversible (no snapshot was taken in the standard
path; the ingester WAL window at risk is ~2h Mimir / ~5min Loki / ~30s
Tempo, all of which flushed to S3 well before the sweep).

If you want belt-and-suspenders snapshots, run before the PVC delete:

```sh
for vol in $(kubectl get pvc -n mimir -o jsonpath='{range .items[?(@.metadata.name~"zone-")]}{.spec.volumeName}{"\n"}{end}' \
  | xargs -I{} kubectl get pv {} -o jsonpath='{.spec.csi.volumeHandle}{"\n"}'); do
  aws ec2 create-snapshot --volume-id $vol --description "pre-sweep $(date +%F)" \
    --tag-specifications "ResourceType=snapshot,Tags=[{Key=iit-billing-tag,Value=lgtm-sweep},{Key=PerconaKeep,Value=False}]" \
    --profile percona-dev-admin --region us-east-1
done
```

The snapshots auto-delete via the cleanup Lambda after 7 days
(`PerconaKeep=False`).

## Out-of-band caveat

The kubectl/aws commands here mutate cluster and AWS state without a
corresponding code change in this repo. This violates the repo's
"all changes via code" convention (see `CLAUDE.md`). The exception is
acceptable because:

1. The orphans being swept are the *result* of a code change that
   already shipped (the zone-aware -> flat collapse).
2. ArgoCD has no representation of the orphan resources to drift against.
3. The sweep is one-shot, not ongoing.

After ADR 0020's retention-policy values shipped, this runbook should
not need to run again for routine operations. Reach for it only if
legacy orphans surface or a future chart-default surfaces a similar
class.
