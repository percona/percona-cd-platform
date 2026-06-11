# Canonical LGTM PVC migration across AZs

Use this runbook to relocate canonical (Bound + in-use) LGTM
ingester/store-gateway/compactor/alertmanager PVCs from one AZ to
another. The originating case is the ADR 0023 sweep: 7 PVCs landed
in us-east-1{b,c} before the [ADR 0020](../adr/0020-lgtm-single-az-collapse.md)
1a pin took effect on Karpenter consolidation, leaving the stack
one reschedule away from `Pending` on a wrong-AZ EBS volume.

This is **not** the orphan-sweep runbook. See
[`lgtm-orphan-pvc-sweep.md`](lgtm-orphan-pvc-sweep.md) for PVCs that
already have no owning Pod. The canonical migration deletes a
Bound + in-use PVC, which is destructive: the ingester WAL goes with
it. Every step gates on positive proof that the WAL was flushed to S3.

## Scope and trade-offs

- Per-stack ingest pause for the duration of one component's flow
  (~5-15 min depending on WAL depth). Single-member rings = no failover.
- Mimir WAL window: ~2h of unflushed samples are at risk if flush
  evidence is not confirmed before PVC delete. The `/ingester/prepare-shutdown`
  procedure (POST) flips `flush_blocks_on_shutdown` + `unregister_on_shutdown`
  for that one shutdown so SIGTERM ships pending TSDB head blocks to S3.
- Loki WAL window: ~5min. Confirm `flush_on_shutdown` is effective
  (chart default `true`; verify via `/ingester/flush_shutdown` if available
  on the running version).
- Tempo WAL window: ~30s. `/shutdown` POST flushes the open block and
  leaves the ring.
- Master-side push pipeline: Alloy `prometheus.remote_write` and
  `loki.write` queues buffer during the pause. Confirm queue capacity
  per master before starting (ADR 0013).

## Order

Lowest risk first, highest WAL window last. Each component completes
before the next starts.

1. Mimir compactor (no WAL; scratch only)
2. Mimir store-gateway (block-list cache; rebuilds from S3)
3. Mimir alertmanager (silences + NFLog in S3 via `alertmanager_storage`)
4. Tempo ingester (~30s WAL)
5. Loki ingester (~5min WAL)
6. Mimir ingester (~2h WAL, biggest blast radius)

## Pre-flight (per component, every time)

```sh
NS=mimir      # one of: mimir, loki, tempo
COMP=ingester # one of: ingester, store-gateway, compactor, alertmanager
APP=$NS       # the ArgoCD app name

# 1. ArgoCD self-heal state (default is on; this migration suspends it temporarily)
kubectl -n argocd get application $APP -o jsonpath='{.spec.syncPolicy.automated}'; echo

# 2. Ring is healthy (no LEAVING/PENDING/UNHEALTHY members)
kubectl -n $NS port-forward svc/${NS}-${COMP} 8080:8080 &
PF=$!
sleep 2
curl -fsS http://localhost:8080/ingester/ring | grep -E 'state":"(LEAVING|PENDING|UNHEALTHY)"' && echo "ABORT: ring unhealthy"
kill $PF 2>/dev/null

# 3. Master-side push queue depth (Mimir only; gate at <50%)
just check-master-ingest

# 4. PV zone (confirm the migration is actually needed for this PVC)
PVC=$(kubectl -n $NS get sts ${NS}-${COMP} -o jsonpath='{.spec.volumeClaimTemplates[0].metadata.name}')-${NS}-${COMP}-0
kubectl -n $NS get pvc $PVC -o jsonpath='{.spec.volumeName}' \
  | xargs -I{} kubectl get pv {} -o jsonpath='{.spec.nodeAffinity.required.nodeSelectorTerms[0].matchExpressions[?(@.key=="topology.ebs.csi.aws.com/zone")].values[0]}'
```

Abort if any pre-flight check fails. Investigate before retrying.

## Per-component procedure

### Step 1 — Cordon non-1a stateful nodes

Forces Karpenter to provision a new 1a node when the replacement Pod
schedules. Without cordoning, a hot reschedule can re-bind the new PVC
to a surviving 1b/1c stateful node.

```sh
kubectl get nodes -l workload.percona.com/tier=lgtm-stateful \
  -o custom-columns=NAME:.metadata.name,ZONE:.metadata.labels.topology\\.kubernetes\\.io/zone \
  | awk '$2 != "us-east-1a" {print $1}' \
  | xargs -r -n1 kubectl cordon
```

### Step 2 — Suspend ArgoCD self-heal for the app

ArgoCD reconciles every 120s (cluster default). A `kubectl scale sts
--replicas=0` will be reverted before the WAL flush completes.

```sh
kubectl -n argocd patch application $APP --type merge \
  -p '{"spec":{"syncPolicy":{"automated":null}}}'
# Verify
kubectl -n argocd get application $APP -o jsonpath='{.spec.syncPolicy.automated}'; echo
```

### Step 3 — Initiate prepare-shutdown (Mimir ingester only)

Mimir's `/ingester/prepare-shutdown` flips both
`flush_blocks_on_shutdown` and `unregister_on_shutdown` for the next
SIGTERM. Without it, the ingester keeps its ring entry and leaves the
WAL unflushed; deleting the PVC then loses the WAL window.

```sh
kubectl -n mimir port-forward svc/mimir-ingester 8080:8080 &
PF=$!
sleep 2
# POST starts the procedure; 204 means complete, 200 means in progress
curl -fsS -X POST http://localhost:8080/ingester/prepare-shutdown -o /dev/null -w '%{http_code}\n'
# Poll until 204 or check logs
until curl -fsS -X GET http://localhost:8080/ingester/prepare-shutdown -o /dev/null -w '%{http_code}\n' | grep -q 204; do
  sleep 5
done
kill $PF 2>/dev/null
```

For Loki ingester, the equivalent is `POST /ingester/flush_shutdown`
(legacy `/ingester/shutdown` on older builds). For Tempo, `POST /shutdown`.

For Mimir compactor / store-gateway / alertmanager, no prepare-shutdown
exists; data is in S3 already. Skip Step 3 for those.

### Step 4 — Scale STS to 0

```sh
kubectl -n $NS scale sts ${NS}-${COMP} --replicas=0
# Wait for Pod to terminate
kubectl -n $NS wait pod -l app.kubernetes.io/component=$COMP --for=delete --timeout=600s
```

### Step 5 — Confirm flush evidence in logs (Mimir/Loki/Tempo ingesters)

```sh
# Mimir: look for "flushing TSDB blocks", "uploading block", "ingester shutdown"
kubectl -n $NS logs --previous ${NS}-${COMP}-0 | grep -E '(flushing|uploaded|shutdown completed)'

# Loki: look for "flushing all in memory chunks"
kubectl -n $NS logs --previous ${NS}-${COMP}-0 | grep -E '(flushing|chunk uploaded|shutting down)'

# Tempo: look for "flushing complete block"
kubectl -n $NS logs --previous ${NS}-${COMP}-0 | grep -E '(flushing|complete block|shutdown)'
```

Abort if the expected lines are missing. The PVC delete in Step 6 is
irreversible.

### Step 6 — Delete the PVC

With `whenScaled: Delete` (ADR 0020) the PVC was already auto-deleted
when the STS scaled to 0. Confirm:

```sh
kubectl -n $NS get pvc | grep ${NS}-${COMP}   # should be empty
```

If a PVC is still Bound (e.g., `whenScaled: Retain` was flipped on a
sibling component), delete it explicitly:

```sh
kubectl -n $NS delete pvc <pvc-name>
```

The PV reclaim is `Delete`, so the underlying EBS volume goes with it.

### Step 7 — Scale STS back to 1

```sh
kubectl -n $NS scale sts ${NS}-${COMP} --replicas=1
```

The new Pod's PVC binds via `WaitForFirstConsumer`. With non-1a
stateful nodes cordoned (Step 1) and `lgtm-stateful` Karpenter pool
pinned to 1a, Karpenter provisions a 1a node and the new PV lands
there.

### Step 8 — Verify the new PV is in 1a

```sh
PVC=storage-${NS}-${COMP}-0   # or data-${NS}-${COMP}-0 for Loki/Tempo; adjust per chart
kubectl -n $NS get pvc $PVC -o jsonpath='{.spec.volumeName}' \
  | xargs -I{} kubectl get pv {} -o jsonpath='{.spec.nodeAffinity.required.nodeSelectorTerms[0].matchExpressions[?(@.key=="topology.ebs.csi.aws.com/zone")].values[0]}'
# Expect: us-east-1a
```

### Step 9 — Wait for Ready + ring ACTIVE

```sh
kubectl -n $NS wait pod ${NS}-${COMP}-0 --for=condition=Ready --timeout=600s

kubectl -n $NS port-forward svc/${NS}-${COMP} 8080:8080 &
PF=$!
sleep 2
curl -fsS http://localhost:8080/ingester/ring | grep -E 'state":"ACTIVE"' || echo "ABORT: ring not ACTIVE"
kill $PF 2>/dev/null
```

### Step 10 — Re-enable ArgoCD self-heal

```sh
kubectl -n argocd patch application $APP --type merge \
  -p '{"spec":{"syncPolicy":{"automated":{"selfHeal":true,"prune":true}}}}'
kubectl -n argocd get application $APP -o jsonpath='{.status.sync.status}'; echo
```

### Step 11 — Verify ingest resumes

```sh
just check-master-ingest
just verify-observability --skip-master
```

Move to the next component only after these pass.

## Post-migration (after all 7 components migrated)

### Drain non-1a stateful nodes

```sh
kubectl get nodes -l workload.percona.com/tier=lgtm-stateful \
  -o custom-columns=NAME:.metadata.name,ZONE:.metadata.labels.topology\\.kubernetes\\.io/zone \
  | awk '$2 != "us-east-1a" {print $1}' \
  | xargs -r -n1 kubectl drain --ignore-daemonsets --delete-emptydir-data
```

Karpenter consolidates the drained nodes within `consolidateAfter: 5m`.

### Optional: add `allowedTopologies: us-east-1a` to the default `gp3` SC

Locks the SC so future PVCs cannot land outside 1a even if a stale
non-1a node ever appears on the stateful pool. The SC `parameters`
field is immutable; this requires `kubectl delete sc gp3` followed by
ArgoCD resync of the `storageclass-gp3` Application. Existing Bound
PVs are unaffected. Schedule outside the migration window; do not
combine.

## Rollback

The PVC delete is irreversible. The acceptable loss per component:

- Mimir compactor / store-gateway / alertmanager: none (S3-backed)
- Mimir ingester: up to ~2h of in-flight samples (one TSDB head block window)
- Loki ingester: up to ~5min of in-flight log lines
- Tempo ingester: up to ~30s of in-flight spans

If Step 5 (flush evidence) fails, do not proceed to Step 6. Scale the
STS back to 1, let it recover, investigate why the flush did not run.

## Out-of-band caveat

This procedure mutates STS replica count and ArgoCD `syncPolicy`
without a corresponding code change, then reverts. Same exception as
[`lgtm-orphan-pvc-sweep.md`](lgtm-orphan-pvc-sweep.md) §Out-of-band:
the mutations are transient, one-shot, and revert to the committed
state within the same window.
