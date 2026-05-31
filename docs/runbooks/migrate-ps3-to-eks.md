# Migrate a Jenkins master to EKS (ps3 = the reference / testbed)

`ps3` is the **first** master moved in-cluster and the **permanent testbed** for
migrating the rest. This runbook is the validated recipe (not a plan): it records
exactly what worked and the gotchas that bite, so the next master is mechanical.

End state: the in-cluster controller boots from the master's **real**
`JENKINS_HOME` (restored from an app-consistent EBS snapshot) and serves the real
host (`ps3.cd.percona.com`) over the shared `jenkins-masters` ALB. The EC2 master
is **fenced, not destroyed** (single-writer), kept as a one-command rollback.

## Substrate prerequisites (already in place for us-east-1a)

- `jenkins_master` EKS **managed** node group (m6a.xlarge, us-east-1a, taint
  `workload.percona.com/tier=jenkins-master`). This is a STATIC MNG, deliberately
  outside Karpenter: no NodePool templates the `jenkins-master` tier value, so the
  controller can only land here. If the node dies the MNG ASG replaces it (brief
  singleton gap), not Karpenter.
- EKS **Pod Identity** association on (ns `jenkins-<host>`, sa `jenkins`) for the
  EC2-plugin AWS creds.
- `gp3-jenkins-1a-retain` StorageClass: `encrypted=true` (account-default EBS key),
  us-east-1a, `reclaimPolicy: Retain`.
- HYBRID controller image (`percona-cd/jenkins-percona`): the two Percona forks
  (`ec2`, `hetzner-cloud`) are baked as `.jpi.override` and FORCED over whatever
  the restored home carries; community plugins stay soft (image is the floor).

## Phase A — fence the source master (single writer) + app-consistent snapshot

The in-cluster controller must be the ONLY writer to the home. Fence the EC2
master first, over SSM (its public DNS is the ALB, so `paws ssh` won't reach it;
use SSM or the private IP over the peering):

```bash
# Pre-flight: no running builds (jenkins CLI has a token even on a 403 master)
jenkins admin -i ps3 groovy -e 'println Jenkins.instance.computers.sum{ (it.executors+it.oneOffExecutors).count{e->e.isBusy()} }'

# Fence: stop + MASK jenkins (mask is required — userdata re-enables it on boot).
# Keep the INSTANCE running: a running-but-masked master is a seconds-fast rollback
# (unmask+start), far better than capacity-0 (which would terminate it).
aws ssm send-command --instance-ids <i-...> --document-name AWS-RunShellScript \
  --parameters commands='["sudo systemctl disable --now jenkins","sudo systemctl mask jenkins"]'
```

App-consistent snapshot (the home volume is XFS, the JENKINS_HOME is a SUBDIR of
the mount — see gotchas). Freeze the **mountpoint**, not the subdir, during the
`create-snapshot` API call, with an auto-thaw timer as a backstop:

```bash
# arm auto-thaw, then freeze /mnt (the data-volume mountpoint)
aws ssm send-command ... commands='["sudo systemd-run --on-active=240 bash -c \"fsfreeze -u /mnt\"","sync","sudo fsfreeze -f /mnt"]'
aws ec2 create-snapshot --volume-id <data-vol> --description "<host> app-consistent"   # while frozen
aws ssm send-command ... commands='["sudo fsfreeze -u /mnt"]'
```

`ps3.cd` is 503 from here until Phase C cutover (proxy still points at the fenced
master). Acceptable for staging; minimise the window by prepping Phase B in
parallel with the slow snapshot copy.

## Phase B — restore the volume, prepare it, boot the controller

```bash
# Copy cross-region ENCRYPTED with the account-default EBS key (matches the SC, so
# the EKS node can attach it). Do NOT tag workload=jenkins (AWS Backup auto-selects
# that tag -> a 14-day vault would leak prod creds). Then create the gp3 volume in
# the controller's AZ.
aws ec2 copy-snapshot --source-region <src> --source-snapshot-id <snap> --region us-east-1 --encrypted
aws ec2 create-volume --region us-east-1 --availability-zone us-east-1a --snapshot-id <copy> --volume-type gp3 --iops 3000 --throughput 125
```

Static PV + PVC (uncommitted — the EBS volume id stays out of this public repo).
**`fsType: xfs`** (the EC2 master formats the data volume XFS; ext4 would fail to
mount) and a `claimRef` so it binds to our PVC:

```yaml
# PV: csi.fsType: xfs ; nodeAffinity zone us-east-1a ; claimRef -> the PVC below
# PVC: storageClassName gp3-jenkins-1a-retain ; volumeName <the PV>
```

**One-time ownership fix — as a Job, NOT an initContainer (the load-bearing
gotcha).** The controller pod is `runAsNonRoot: true`, so a root chown
initContainer is rejected (`CreateContainerConfigError: runAsUser breaks non-root
policy`). The namespace has no PSS enforce label, so run a SEPARATE root Job that
mounts the PVC and chowns the restored home (EC2 jenkins UID is 994; in-cluster is
1000). Scale the controller to 0 first to release the RWO PVC:

```bash
kubectl -n jenkins-<host> scale sts jenkins-<host> --replicas=0   # release the PVC
# Job (busybox, runAsUser 0, ns allows it): guard config.xml+master.key, then
#   chown -R 1000:1000 /data/<host-home-subdir>     # ~580k files on ps3, ~40s
```

Per-host values (`instances/<host>/values.yaml`):

```yaml
jenkins:
  controller:
    customInitContainers:        # NON-root boot-guard only (chown is the Job above)
      - name: boot-guard
        image: public.ecr.aws/docker/library/busybox:1.36
        securityContext: { runAsUser: 1000, runAsNonRoot: true }
        command: ["sh","-c"]
        args: ["test -f /var/jenkins_home/config.xml && test -f /var/jenkins_home/secrets/master.key"]
        volumeMounts: [{ name: jenkins-home, mountPath: /var/jenkins_home, subPath: <host-home-subdir> }]
  persistence:
    existingClaim: <host>-jenkins-home
    subPath: <host-home-subdir>   # JENKINS_HOME is a SUBDIR of the volume root
```

Then `argocd app sync jenkins-<host> --core` (the controller is manual-sync,
ADR 0025; remember the kube-context namespace must be `argocd` for `--core`).

## Phase C — cutover the real host (reversible overlay)

Apply a STANDALONE Ingress for the real host with
`alb.ingress.kubernetes.io/group.order: "-10"` on the shared `jenkins-masters`
group. It OUTRANKS the jenkins-ingress proxy's rule for the same host by ALB
priority, so the real host resolves to the controller directly (ALB sets a clean
`X-Forwarded-Proto=https` for OAuth) with NO DNS change. Delete the Ingress to roll
back. Verify: `aws elbv2 describe-rules` shows the controller TG winning,
`curl -sI https://<host>/login` returns 200.

## Phase D — validate

GitHub OAuth login (ALB forwards `X-Forwarded-Proto=https`); HYBRID plugin set
(forks at the locked versions win over the restored copies, community survive);
one real EC2 (spot) + one Hetzner worker provision and connect; a representative
job runs green. NB: the worker SGs may only allow the OLD master VPC — add SSH
ingress from the EKS VPC CIDR before provisioning.

## Retire the proxy/reconciler entry (LAST, gated on "fully in place")

Only once the controller permanently serves the real host: remove the `<host>`
entry from BOTH `jenkins-endpoint-reconciler/values.yaml` and
`jenkins-ingress/values.yaml` (ArgoCD prunes the proxy Ingress rule + the
`jenkins-<host>` Service/EndpointSlice). Until then the reconciler is the standing
rollback path, so leave it running.

## Rollback (any time before retiring the proxy entry)

```bash
kubectl -n jenkins-<host> delete ingress jenkins-<host>-cutover     # real host falls back to the proxy
aws ssm send-command --instance-ids <i-...> ... commands='["sudo systemctl unmask jenkins","sudo systemctl enable --now jenkins"]'
```

The reconciler already points the proxy at the (now-unmasked) EC2 master, so the
real host serves from EC2 again within a reconcile cycle.

## Gotchas (the behaviors this testbed surfaced)

- **runAsNonRoot pod ⇒ chown is a Job, not an initContainer.** A root
  initContainer dies with `CreateContainerConfigError`. Do the one-time chown in a
  separate root Job against the PVC (the namespace allows root pods; the master pod
  does not).
- **`fsGroupChangePolicy: OnRootMismatch`** is already set by the chart, but the
  FIRST boot on a 994-rooted home still does a full recursive kubelet ownership
  pass (`VolumePermissionChangeInProgress`, ~580k files on ps3, several minutes).
  Pre-chowning via the Job sets root to 1000, so that pass — and every later boot's
  — is skipped. Pre-chown BEFORE first boot to avoid the double pass entirely.
- **XFS + subPath.** The data volume is XFS (PV `fsType: xfs`), and JENKINS_HOME is
  a subdir of the mount (`persistence.subPath`), or Jenkins boots the empty volume
  root and ignores `config.xml`. The chart honors `persistence.subPath` on every
  jenkins-home mount.
- **Data volume `DeleteOnTermination=false`** — verify before any terminate so a
  fence/teardown can never delete the real home. Root device is separate/expendable.
- **Encrypt the cross-region copy with the account-default EBS key** (no explicit
  kms-key-id) to match the SC, or the node cannot attach the volume.
- **Don't tag the clone volume/snapshot `workload=jenkins`** (AWS Backup vault leak).
- **Singleton availability:** the MNG is min=desired=max=1; node loss = a short gap
  while the ASG replaces it. Consider a warm-standby story before busy masters.
