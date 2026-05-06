# ArgoCD bootstrap — GitOps Bridge pattern.
#
# Three resources, in strict order:
#   1. helm_release.argocd       — argo-cd chart in HA topology
#   2. kubernetes_secret_v1      — cluster Secret with annotations carrying
#                                  TF outputs into ApplicationSet valuesObject
#   3. kubectl_manifest.root_app — root Application of-Apps; ArgoCD then
#                                  walks argocd-bootstrap/ on its own
#
# After this point Terraform stops touching the cluster — every addon,
# Jenkins host, dashboard, and rule is reconciled by ArgoCD from the
# resources/ tree of this repo.

resource "helm_release" "argocd" {
  name             = "argocd"
  namespace        = "argocd"
  create_namespace = true
  repository       = local.charts.argo_cd.repo
  chart            = local.charts.argo_cd.name
  version          = local.charts.argo_cd.ver

  # HA topology. server.ingress is intentionally disabled — ArgoCD will
  # create its own Ingress later via the addons ApplicationSet, once
  # aws-load-balancer-controller and external-dns are healthy. Going through
  # GitOps for the Ingress means a single source of truth for ALB groups,
  # health checks, and ACM ARN reference.
  values = [yamlencode({
    global = {
      domain = var.argocd_hostname
      # Every ArgoCD component gets the system-critical priority class so
      # Karpenter consolidation never preempts the GitOps engine. Class
      # is created by resources/addons/priorityclasses/ at sync-wave -100.
      priorityClassName = "platform-system-critical"
    }
    controller = {
      replicas = 1 # leader-election; chart does not yet support active-active
      pdb = {
        enabled = true
      }
    }
    "redis-ha" = {
      enabled = true
    }
    server = {
      replicas = 2
      pdb = {
        enabled      = true
        minAvailable = 1
      }
      ingress = {
        enabled = false
      }
    }
    repoServer = {
      replicas = 2
      pdb = {
        enabled      = true
        minAvailable = 1
      }
    }
    applicationSet = {
      replicas = 2
      pdb = {
        enabled      = true
        minAvailable = 1
      }
    }
    # Pin every ArgoCD pod to the system NG so addon churn (Karpenter
    # scale-from-zero, spot-replacement) never evicts the GitOps engine.
    nodeSelector = {
      "node-role" = "system"
    }
  })]

  # coredns + kube-proxy must be running before any pod can resolve cluster
  # DNS or reach a Service IP. Pod Identity agent must be running before any
  # pod that would normally use it (ArgoCD itself does not, but downstream
  # addons absolutely do, and we don't want the chain ever broken).
  depends_on = [
    aws_eks_addon.coredns,
    aws_eks_addon.kube_proxy,
    aws_eks_addon.pod_identity_agent,
  ]
}

# Cluster Secret — ArgoCD identifies clusters by `argocd.argoproj.io/secret-type: cluster`
# label. Annotations carry TF outputs that downstream ApplicationSet templates
# pull in via `{{ metadata.annotations.<key> }}`. Annotation keys MUST match
# the references in argocd-bootstrap/applicationsets/addons.yaml.
resource "kubernetes_secret_v1" "argocd_cluster" {
  metadata {
    name      = local.cluster_name
    namespace = helm_release.argocd.namespace
    labels = {
      "argocd.argoproj.io/secret-type" = "cluster"
      "enabled"                        = "true"
    }
    annotations = {
      # Cluster identity
      cluster_name      = module.eks.cluster_name
      aws_account_id    = data.aws_caller_identity.current.account_id
      aws_region        = var.aws_region
      oidc_provider_arn = module.eks.oidc_provider_arn
      route53_zone_id   = local.route53_zone_id

      # VPC ID — consumed by the AWS LBC chart (`vpcId` arg) so the controller
      # skips the IMDS query path. Required because the system NG launch
      # template enforces IMDSv2 hop_limit=1 (eks.tf hardening #10), which
      # blocks the controller's pod-side IMDS metadata fetch.
      cluster_vpc_id = module.vpc.vpc_id

      # ACM
      acm_wildcard_arn = module.acm.acm_certificate_arn

      # Hostnames — single source of truth is var.* in terraform/variables.tf.
      # Addon ApplicationSets pull these into Helm valuesObject so chart
      # values.yaml never carries a hardcoded *.cd.percona.com.
      argocd_hostname  = var.argocd_hostname
      grafana_hostname = var.grafana_hostname

      # Pod Identity role ARNs (consumed by addon Helm valuesObject)
      alb_controller_role_arn   = module.pod_identity_alb_controller.iam_role_arn
      external_dns_role_arn     = module.pod_identity_external_dns.iam_role_arn
      ebs_csi_role_arn          = module.pod_identity_ebs_csi.iam_role_arn
      external_secrets_role_arn = module.pod_identity_external_secrets.iam_role_arn

      # Karpenter — controller role + interruption queue + node role
      karpenter_iam_role_arn      = module.karpenter.iam_role_arn
      karpenter_sqs_name          = module.karpenter.queue_name
      karpenter_node_iam_role_arn = module.karpenter.node_iam_role_arn

      # LGTM — bucket names + Pod Identity role ARNs. Each component reads
      # only the matching annotation in its chart's values.yaml; unrelated
      # keys are ignored (Helm tolerates extras).
      mimir_bucket_name = aws_s3_bucket.lgtm["mimir"].bucket
      loki_bucket_name  = aws_s3_bucket.lgtm["loki"].bucket
      tempo_bucket_name = aws_s3_bucket.lgtm["tempo"].bucket
      mimir_role_arn    = module.pod_identity_mimir.iam_role_arn
      loki_role_arn     = module.pod_identity_loki.iam_role_arn
      tempo_role_arn    = module.pod_identity_tempo.iam_role_arn
      lgtm_kms_key_arn  = aws_kms_key.lgtm.arn

      # External-push hostnames for the Alloy gateway (resources/addons/
      # alloy-gateway/). These resolve via external-dns to the shared ALB.
      lgtm_push_mimir = var.lgtm_push_hostnames.mimir
      lgtm_push_loki  = var.lgtm_push_hostnames.loki
      lgtm_push_tempo = var.lgtm_push_hostnames.tempo

      # Authentik bridge (HD-30780, replaces direct Grafana SAML since
      # Grafana OSS lacks SAML support). Authentik talks SAML to Duo as
      # the SP, exposes OIDC inward to Grafana / future Jenkins masters.
      # IdP metadata is base64-encoded for safe transit through the
      # annotation → ApplicationSet valuesObject → Helm values pipeline
      # (multi-line XML survives base64 cleanly).
      authentik_hostname             = var.authentik_hostname
      authentik_saml_enabled         = tostring(var.authentik_saml_enabled)
      authentik_saml_idp_metadata_b64 = base64encode(local.authentik_saml_idp_metadata)

      # Grafana OIDC client config — Grafana points at Authentik's OIDC
      # endpoints. The client_secret itself flows through ESO (not the
      # cluster-Secret annotation): TF provisions it in Secrets Manager
      # under percona-ci-platform/authentik/config (see authentik.tf),
      # and the Grafana chart wrapper's external-secret-oidc.yaml
      # template syncs it into a Secret that Grafana mounts as env.
      grafana_oidc_issuer_url = "https://${var.authentik_hostname}"
      grafana_oidc_client_id  = "grafana"
    }
  }

  data = {
    # ArgoCD's required cluster-secret schema. `name` shows up in the UI;
    # `server` is the API endpoint (in-cluster shorthand); `config` is the
    # auth/TLS payload — empty-tls is correct for the in-cluster pseudo-cluster.
    name   = local.cluster_name
    server = "https://kubernetes.default.svc"
    config = jsonencode({
      tlsClientConfig = {
        insecure = false
      }
    })
  }

  type = "Opaque"

  depends_on = [helm_release.argocd]
}

# Root App-of-Apps. Lives in the built-in `default` AppProject so it can
# create the `platform` AppProject (from argocd-bootstrap/projects/platform.yaml)
# without a chicken-and-egg. Once the root syncs, the platform project exists
# and every downstream Application uses it.
resource "kubectl_manifest" "argocd_root_app" {
  yaml_body  = file("${path.module}/../argocd-bootstrap/root-app.yaml")
  depends_on = [kubernetes_secret_v1.argocd_cluster]
}
