# Owner: platform
# Operational and derived values only. Version pins live in versions.tf
# (engine, providers, and the local.modules / local.charts maps), the single
# pin manifest verified by `just check-versions`. Looking for
# local.modules.<x>.source? That is versions.tf, not here.
locals {
  cluster_name = var.cluster_name
  region       = var.aws_region

  # Inherits var.tags via provider default_tags. Modules that take their own
  # `tags` argument should pass `local.tags` so the merged set is consistent.
  tags = var.tags

  # State bucket name — single source of truth referenced by docs/runbooks/.
  # Native S3 locking (use_lockfile in backend.tf) writes a sibling .tflock
  # object next to the state file; no separate lock service needed.
  state_bucket = "terraform-state-storage-${local.cluster_name}"

  # Resolved Route 53 ID for the platform zone. Cached locally so all consumers
  # (pod-identity, acm, argocd) reference the same value.
  route53_zone_id = data.aws_route53_zone.main.zone_id

  # Authentik SAML IdP metadata. Source of truth is an SSM Parameter at
  #   /${cluster_name}/authentik/saml/idp_metadata
  # populated by the operator with `aws ssm put-parameter` (one-time;
  # rotates only when Duo's signing cert rotates). TF reads it at apply
  # time via data.aws_ssm_parameter.authentik_saml_idp_metadata, base64-
  # encodes into the cluster-Secret annotation, and the Authentik chart
  # wrapper decodes into a ConfigMap that Authentik mounts. Empty string
  # when SAML is disabled — chart wrapper template no-ops in that case.
  #
  # Why SSM (not Secrets Manager): IdP metadata is public config —
  # Duo's signing cert is the public half of the IdP signature. SSM
  # ParameterStore is the right home for this; Secrets Manager stays
  # for the SP private key + cert (which ARE secrets).
  #
  # Why TF data source (not ESO): metadata rotates ~yearly, well-suited
  # to apply-time read; avoids granting ESO ssm:GetParameter and the
  # extra ClusterSecretStore.
  authentik_saml_idp_metadata = (
    var.authentik_saml_enabled
    ? data.aws_ssm_parameter.authentik_saml_idp_metadata[0].value
    : ""
  )

  # NodeGroup sizing knobs surfaced as locals so they're easy to find.
  ng = {
    system = {
      # Non-burstable Graviton for the bootstrap addons: no credit dynamics
      # (after the 2026-05-11 t3.medium credit-starvation outage). 3 across AZs
      # for argocd/karpenter quorum; multi-family covers per-AZ capacity gaps.
      instance_types = ["m7g.large", "m8g.large"]
      min_size       = 3
      desired_size   = 3
      max_size       = 4
    }
    prometheus_system = {
      # Non-burstable Graviton for the obs-state singletons (Grafana,
      # Authentik PG, mtr-pg); multi-family covers per-AZ capacity.
      instance_types = ["m7g.large", "m8g.large"]
      min_size       = 1
      desired_size   = 1
      max_size       = 2
    }
    jenkins_master = {
      # Single dedicated 1a node for the in-cluster Jenkins controller (ps3-k8s).
      # m7g/m8g.xlarge (4 vCPU / 16 GiB) clears the controller's 6Gi/4000m limit
      # plus DaemonSets. Single node (min=desired=max=1): the controller is a
      # single-replica SPOF, so an arch/AMI change recreates the one node in a
      # window and the Retain PVC re-attaches in the same AZ.
      instance_types = ["m7g.xlarge", "m8g.xlarge"]
      min_size       = 1
      desired_size   = 1
      max_size       = 1
    }
  }

  # ---- Cleanup Lambda parameters ----
  # Tunables for the scheduled cleanup reapers (terraform/{volume,ec2}-cleanup.tf):
  # schedules, dry-run flags, the EKS skip regex, and the volume age floor, all
  # in one reviewable place. The scheduled-lambda module stays generic; these
  # are the workload knobs. Arm a reaper by flipping its dry_run to "false" here
  # (a reviewed edit, not a CLI -var) once its dry-run logs look right.
  cleanup_lambda_runtime = "python3.14" # latest supported Lambda Python runtime

  volume_cleanup = {
    schedule      = "rate(1 day)"
    dry_run       = "false" # armed 2026-06-09 after dry-run review (12 orphans incl 16 TB)
    min_age_hours = "24"
  }

  ec2_cleanup = {
    schedule         = "rate(5 minutes)"
    timeout          = 120
    dry_run          = "false" # armed 2026-06-09 after clean dry-run cycles
    eks_skip_pattern = "pe-.*"
    # Ephemeral package-testing molecule instances: their non-numeric billing
    # tag would exempt them forever, but an aborted build skips `molecule
    # destroy` and leaks them. Tags matching the pattern are age-bounded
    # instead; 7h clears the worst genuine run (4.34h, p99 0.93h over 819
    # builds) while reclaiming leaks.
    molecule_billing_pattern = ".*_package_testing$"
    molecule_max_age_hours   = "7"
  }
}
