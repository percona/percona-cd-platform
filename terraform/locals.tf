# Owner: platform
locals {
  cluster_name = var.cluster_name
  region       = var.aws_region

  # Inherits var.tags via provider default_tags. Modules that take their own
  # `tags` argument should pass `local.tags` so the merged set is consistent.
  tags = var.tags

  # Derived hostnames for external-dns + ALB Ingresses (no hardcoded host strings elsewhere).
  jenkins_friendly_hosts = [for h, _ in var.jenkins_hosts : "${h}.${var.route53_zone_name}"]

  # State bucket name — single source of truth referenced by docs/runbooks/.
  # Native S3 locking (use_lockfile in backend.tf) writes a sibling .tflock
  # object next to the state file; no separate lock service needed.
  state_bucket = "terraform-state-storage-${local.cluster_name}"

  # Resolved Route 53 ID for the platform zone. Cached locally so all consumers
  # (pod-identity, acm, origins, argocd) reference the same value.
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
      # m6a.large (non-burstable) replaces t3.medium after the 2026-05-11
      # outage: kubelet timed out responding to API-server health checks on
      # both AZ-b and AZ-c t3.medium instances because CPUCreditBalance had
      # been pinned at 0 for hours while CPU ran 21-88% (above the 40%
      # t3.medium baseline). Standard credit mode then throttled the
      # instance below kubelet's needs. m6a.large gives 2 vCPU / 8 GiB at a
      # fixed ~$0.0864/hr with no credit dynamics. Sizing 3 across AZs so
      # one-AZ failure leaves quorum for argocd / karpenter / external-secrets.
      instance_types = ["m6a.large"]
      min_size       = 3
      desired_size   = 3
      max_size       = 4
    }
    prometheus_system = {
      instance_types = ["m6a.large"]
      min_size       = 1
      desired_size   = 1
      max_size       = 2
    }
    jenkins_master = {
      # Dedicated single-AZ on-demand node for the in-cluster Jenkins controller
      # pilot (ps3-k8s). m6a.xlarge (4 vCPU / 16 GiB) matches the deleted
      # jenkins_system NG and clears the controller's 6Gi mem / 4000m cpu limit
      # plus the node DaemonSets. Single node (min=desired=max=1): the controller
      # is a single-replica SPOF, so no scaling/surge -- an AMI bump recreates the
      # one node in a window and the Retain PVC re-attaches in the same AZ. Costs
      # one idle node until the pilot deploys (the accepted dark-replica tradeoff,
      # same as the old jenkins_system at ~$126/mo).
      instance_types = ["m6a.xlarge"]
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
