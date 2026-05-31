# Lessons Learned from the POC

Based on the ps57 lift-and-shift (2026-03-28).

## What was easy

**EKS cluster**: one YAML file, `eksctl create cluster`, 15 minutes. Managed
node group, EBS CSI driver, OIDC provider all handled automatically.

**Docker image**: `Dockerfile` + `plugins.txt` generated from the live instance.
Build in minutes. All 155 plugins (including 2 Percona forks) baked in. No more
unmanaged plugins on EBS volumes.

**Data migration**: snapshot the existing EBS volume, create a new volume from
the snapshot, mount as a PVC. Jenkins starts with the exact same jobs,
credentials, and build history. Zero data loss.

**Hetzner workers**: connected back to the EKS-hosted Jenkins via JNLP with no
changes. The Hetzner cloud plugin, templates, and init scripts all worked as-is.
First test build completed in 108 seconds.

**TLS**: ALB + ACM wildcard cert (`*.cd.percona.com`). No OpenResty, no certbot,
no DH params, no renewal cron. Certificate auto-renews. One cert covers all
future Jenkins instances on the cluster.

**Startup time**: ~90 seconds from pod creation to "Jenkins is fully up and
running". Compared to 10+ minutes on EC2 (cloud-init + yum install + chown
100GB + nginx setup + certbot).

**Fresh instance from unified image**: The unified Docker image (249 plugins,
the union of all 10 instances) provides everything Jenkins needs to boot. A
fresh PVC with dynamic provisioning (20GB gp3) boots a fully functional Jenkins
in ~90 seconds. No volume clone needed for new dev/test instances.

**One unified plugin set**: The union of all 10 instances' plugins (249 total)
doesn't cause conflicts. Some plugins are instance-specific (allure on pmm,
ansicolor on pxc, etc.) but having extra plugins installed doesn't break
anything; they're just available if needed.

## What was painful

### EC2 plugin and IRSA (blocker, resolved)

The EC2 plugin (`hudson.plugins.ec2`) with `useInstanceProfileForCredentials=true`
uses `InstanceProfileCredentialsProvider`, which calls EC2 instance metadata
(IMDS). On EKS pods, IMDS resolves to the **node instance role**, not the pod's
IRSA role.

Setting `useInstanceProfileForCredentials=false` with a blank `credentialsId`
does not fall through to `DefaultCredentialsProvider` (which would pick up
IRSA). Instead, the plugin requires a `credentialsId` referencing an
`AWSCredentialsImpl` credential.

`AWSCredentialsImpl` with empty access key/secret throws
`NullPointerException: Credentials must not be null` on startup, causing a crash
loop.

**Root cause (classloader isolation)**: The EC2 plugin's classloader is separate
from `aws-java-sdk2-core`. `DefaultCredentialsProvider` (from the core plugin)
cannot see the STS classes bundled in the EC2 plugin. Adding an STS dependency
does not help because `DefaultCredentialsProvider` uses its own classloader to
discover credential providers, and STS is not on that classpath.

**Fix**: Construct `StsWebIdentityTokenFileCredentialsProvider` explicitly in
the EC2 plugin code (`EC2Cloud.java`) using the EC2 plugin's own classloader.
This bypasses the service discovery mechanism that fails due to classloader
boundaries. The Percona fork (`ec2:5.24.percona.2`) already exists for this.

### cloud.groovy overrides everything on startup

`useInstanceProfileForCredentials` is a final field set during `EC2Cloud`
construction from `config.xml`. However, `cloud.groovy` (in `init.groovy.d/`)
recreates the `EC2Cloud` objects on every Jenkins startup, resetting the field
to whatever is hardcoded in the script.

Editing `config.xml` directly is useless because Jenkins saves its in-memory
state back to `config.xml` during startup, overwriting any manual edits.

**Fix**: A persistent groovy script that runs AFTER `cloud.groovy` in
alphabetical order. Since scripts in `init.groovy.d/` execute alphabetically,
naming the fix script with a prefix like `e-` ensures it runs after `c-`
(`cloud.groovy`). For example, `e-eks-irsa-credentials.groovy`.

**Lesson**: any configuration that `cloud.groovy` sets will be overwritten on
every restart. Post-startup fixup scripts must sort alphabetically after
`cloud.groovy`.

### OAuth callback mismatch

The cloned volume had Google OAuth and GitHub OAuth configured with callback
URLs for `ps57.cd.percona.com`. Jenkins at `ps57-k8s.cd.percona.com` can't
complete the OAuth flow. Had to switch to local Jenkins auth for the POC.

**For production**: add the new domain to the OAuth app's authorized redirect
URIs before cutover.

### init.groovy.d script ordering

Scripts in `init.groovy.d/` run in alphabetical order. The cloned volume's
`matrix.groovy` (GlobalMatrixAuthorizationStrategy) ran after `fix-auth.groovy`,
overwriting the auth changes. Had to remove `matrix.groovy` from the PVC.

**Lesson**: when cloning a volume, audit all `init.groovy.d/` scripts. Some
may conflict with the new environment. Use `z-` prefix for scripts that must
run last, or remove conflicting scripts before first boot.

### Credential crash loop

An `AWSCredentialsImpl` with empty access key was saved to `credentials.xml`
via the Script Console. On the next restart, Jenkins failed to deserialize it
(NPE) and entered a crash loop. The Jenkins container couldn't start, so
Script Console was unavailable.

**Fix**: scaled the StatefulSet to 0, mounted the PVC with a debug pod, edited
`credentials.xml` and `config.xml` directly, scaled back to 1.

**Lesson**: never save credentials via Script Console without testing a restart
first. Have a recovery procedure ready (debug pod + PVC editing).

### Docker image architecture

The remote Docker host (dm3) runs on arm64. The default `docker build` produced
an arm64 image. The EKS node (`t3.xlarge`) is amd64. The image pulled
successfully but failed with "no match for platform in manifest".

**Fix**: `docker buildx build --platform linux/amd64 --push`. Cross-platform
build via QEMU emulation is slow (~5 min for plugin installation under
emulation).

**Lesson**: always specify `--platform` when the build host and target differ.
Or use a multi-arch build.

### ConfigMap volumes vs extraVolumes in Helm chart

The Jenkins Helm chart's `customInitContainers` do NOT have access to
`extraVolumes`. Init containers can only see volumes defined under
`persistence.volumes`, which are added to the pod spec and visible to all
containers including init containers.

**Lesson**: when an init container needs to read from a ConfigMap or Secret,
define the volume under `persistence.volumes`, not `extraVolumes`. The
`extraVolumes` are only mounted into the main Jenkins container.

### TLS policy on ALB

The default ALB security policy allows TLS 1.0 and TLS 1.1, which are
deprecated and insecure. Must explicitly set the annotation:

```
alb.ingress.kubernetes.io/ssl-policy: ELBSecurityPolicy-TLS13-1-2-Res-PQ-2025-09
```

This enforces TLS 1.2 as the minimum with TLS 1.3 support. `Res-PQ-2025-09`
is AWS's recommended default since Sept 2025: it adds hybrid post-quantum
key exchange (ML-KEM, transparent fallback for clients without it) and the
restricted (`Res`) cipher set, dropping the legacy non-forward-secret RSA
ciphers that the older `…-2021-06` policy still carried.

**Lesson**: never rely on ALB defaults for TLS. Always set the ssl-policy
annotation explicitly.

## Infrastructure patterns

### Shared ALB via group.name

Adding `alb.ingress.kubernetes.io/group.name: jenkins-shared` to all ingresses
consolidates them into one ALB with host-based routing. The ALB Ingress
Controller creates separate target groups per hostname automatically.

**Cost savings**: ~$16/mo per additional instance (avoiding a dedicated ALB per
ingress). For 10 instances, that is ~$144/mo saved.

**Lesson**: always use `group.name` when multiple services share the same
domain and certificate.

### VPC peering vs PUBLIC_DNS for worker connectivity

For the single-region POC, VPC peering between the EKS VPC and EC2 worker VPCs
works. Jenkins uses the `PRIVATE_IP` connection strategy to reach workers via
private IPs across peered VPCs.

For multi-region production, public IP SSH (`PUBLIC_DNS` connection strategy)
eliminates all VPC peering complexity. Workers already have public IPs; Jenkins
just needs the NAT gateway EIP whitelisted in worker security groups.

**Recommendation**: use `PUBLIC_DNS` for production. VPC peering across regions
adds complexity (transit gateways, route tables, cross-region costs) that is not
justified when workers already expose public IPs.

### All changes must be in code

Manual `kubectl annotate`, `kubectl patch`, or Script Console commands create
drift between the git repo and the live cluster. Everything must go through
values files, Helm upgrades, and git commits.

The `justfile` is the CLI interface for all operations. Git is the source of
truth. If a change cannot be expressed in a values file or Helm template, it
should be a persistent groovy script or a Dockerfile change, not a manual
command.

## Production rollout considerations

### Per-instance migration

Each Jenkins instance needs:
- Its own `jenkins-values.yaml` (hostname, JVM opts, executor count)
- Its own EBS volume clone (snapshot + PVC), or a fresh PVC if starting clean
- OAuth callback URLs updated before DNS cutover
- Audit of `init.groovy.d/` scripts for environment-specific assumptions
- Post-`cloud.groovy` fixup script if IRSA credentials are needed

### EC2 plugin fix (prerequisite)

All 10 instances use EC2 workers. The IRSA credential gap must be resolved
before any production migration. The fix is to patch the Percona EC2 plugin fork
to construct `StsWebIdentityTokenFileCredentialsProvider` explicitly, bypassing
the classloader isolation issue with `DefaultCredentialsProvider`.

### Shared vs dedicated clusters

The POC uses a dedicated single-node cluster ($250/mo). For production:
- **Shared cluster**: all 10 Jenkins masters on one EKS cluster. EKS control
  plane cost ($73/mo) is amortized. Node group scales based on total resource
  demand. Use shared ALB via `group.name` to save ~$144/mo. Risk: blast radius
  (cluster issue affects all instances)
- **Dedicated clusters**: one cluster per instance. Higher cost ($73/mo each)
  but full isolation. Same as current EC2 model

Recommended: start with a shared cluster for low-traffic instances (ps57, pg,
ps3), keep high-traffic ones (ps80, pmm, psmdb) on dedicated clusters or EC2
until validated.

### DNS cutover strategy

1. Deploy Jenkins on EKS with a temporary domain (`ps57-k8s.cd.percona.com`)
2. Validate: auth, workers, builds, credentials, plugins
3. Update OAuth callback URLs for the production domain
4. Switch DNS (`ps57.cd.percona.com`) to the EKS ALB
5. Decommission the EC2 instance and CF stack

### What NOT to change during migration

- Worker configurations (cloud.groovy, htz.cloud.groovy)
- Pipeline code (Jenkinsfiles, shared libraries)
- Job definitions
- Credentials (carry over on the cloned PVC)
- Plugin versions (baked into the Docker image from the live instance)
