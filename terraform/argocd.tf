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

  # HA topology + Authentik OIDC + ALB Ingress at var.argocd_hostname.
  #
  # Why values stay in TF (not GitOps): ArgoCD bootstraps itself via this
  # helm_release. Putting OIDC config / Ingress in a self-managed addon
  # at resources/addons/argocd/ would create a chicken-and-egg on first
  # bring-up. Keeping it here means TF apply rolls argocd-server with the
  # right cm + new Ingress in one step.
  values = [yamlencode({
    global = {
      domain = var.argocd_hostname
      # Every ArgoCD component gets the system-critical priority class so
      # Karpenter consolidation never preempts the GitOps engine. Class
      # is created by resources/addons/priorityclasses/ at sync-wave -100.
      priorityClassName = "platform-system-critical"
    }
    # Drop the bundled Dex pod entirely — Authentik replaces it.
    dex = {
      enabled = false
    }
    configs = {
      # argocd-cmd-params-cm — argocd-server speaks plain HTTP on 8080
      # since TLS terminates at the ALB. Without this, the chart's default
      # self-signed cert breaks the ALB target-group health check and
      # browsers see 502.
      params = {
        "server.insecure" = true
      }
      # argocd-cm — Authentik OIDC config + URL.
      # admin.enabled: false — local 'admin' login form disabled after
      # SSO was validated end-to-end (browser flow + CLI). SSO-only path.
      #
      # Recovery if Authentik / Duo / SAML SP cert is broken:
      #   kubectl -n argocd patch cm argocd-cm --type merge \
      #     -p '{"data":{"admin.enabled":"true"}}'
      #   kubectl -n argocd rollout restart deploy/argocd-server
      #   # then reset the admin password via the bcrypt'd argocd-secret:
      #   kubectl -n argocd patch secret argocd-secret --type=json \
      #     -p '[{"op":"remove","path":"/data/admin.password"},
      #          {"op":"remove","path":"/data/admin.passwordMtime"}]'
      #   kubectl -n argocd rollout restart deploy/argocd-server
      #   # the new admin password prints to argocd-server stdout on boot
      cm = {
        url             = "https://${var.argocd_hostname}"
        "admin.enabled" = "false"
        "oidc.config"   = <<-OIDC
          name: Authentik
          issuer: https://${var.authentik_hostname}/application/o/argocd/
          clientID: argocd
          clientSecret: $argocd-oidc:client_secret
          requestedScopes:
            - openid
            - profile
            - email
          requestedIDTokenClaims:
            groups:
              essential: true
        OIDC
      }
      # argocd-rbac-cm — group → role mapping. Reuses the Grafana group
      # naming for now (single bootstrap-phase admin cohort: Anderson,
      # Alex Miroshnychenko, Vadim Yalovets in grafana_cd_admins). Split
      # into dedicated argocd_cd_admins / argocd_cd_users via Santiago
      # later if the cohorts diverge.
      #
      # policy.default: '' (deny by default) — unmapped users get nothing.
      # scopes: '[groups]' — ArgoCD reads the OIDC `groups` claim only.
      rbac = {
        create           = true
        scopes           = "[groups]"
        "policy.default" = ""
        "policy.csv"     = <<-RBAC
          g, grafana_cd_admins, role:admin
          g, percona, role:readonly
        RBAC
      }
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
      # Single Ingress, dual-Service via the chart's `controller: aws`
      # mode: the chart auto-generates an `argogrpc` synthetic Service
      # with backend-protocol-version=GRPC, and the Ingress condition-
      # routes Content-Type=application/grpc to it (CLI gRPC), everything
      # else to argocd-server (UI/REST). Single hostname covers both.
      #
      # Joins the existing jenkins-cd ALB IngressGroup — same ALB as
      # Authentik and Grafana, ACM wildcard *.cd.percona.com already
      # covers the host, external-dns auto-creates the Route53 record.
      #
      # Sticky sessions are NOT needed here: argocd-server is stateless
      # OIDC-wise (state lives in the argocd.token cookie + OAuth state
      # param). Different from Authentik's flow which needs in-memory
      # affinity.
      ingress = {
        enabled          = true
        controller       = "aws"
        ingressClassName = "alb"
        hostname         = var.argocd_hostname
        path             = "/"
        pathType         = "Prefix"
        tls              = true
        aws = {
          backendProtocolVersion = "GRPC"
          serviceType            = "NodePort"
          # Per-TG annotations on the auto-generated argocd-server-grpc
          # Service. AWS LBC's per-Service annotations override the
          # Ingress-level ones for that specific target group. Required
          # because the gRPC TG can't accept httpCode 200 (it expects
          # grpcCode in 0-99). Setting success-codes "0" on the Service
          # makes the gRPC TG happy; the HTTP TG keeps its httpCode 200
          # from the Ingress-level success-codes annotation.
          serviceAnnotations = {
            "alb.ingress.kubernetes.io/success-codes" = "0"
          }
        }
        annotations = {
          "alb.ingress.kubernetes.io/group.name"       = "jenkins-cd"
          "alb.ingress.kubernetes.io/scheme"           = "internet-facing"
          "alb.ingress.kubernetes.io/target-type"      = "ip"
          "alb.ingress.kubernetes.io/listen-ports"     = "[{\"HTTPS\":443}]"
          "alb.ingress.kubernetes.io/ssl-policy"       = "ELBSecurityPolicy-TLS13-1-2-Res-PQ-2025-09"
          "alb.ingress.kubernetes.io/backend-protocol" = "HTTP"
          "alb.ingress.kubernetes.io/healthcheck-path" = "/healthz"
          "alb.ingress.kubernetes.io/healthcheck-port" = "traffic-port"
          "alb.ingress.kubernetes.io/success-codes"    = "200"
        }
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
      "workload.percona.com/tier" = "bootstrap"
    }
    # Bootstrap-tier toleration paired with the Stage 2b
    # CriticalAddonsOnly:NoSchedule taint that will isolate the system
    # MNG. Inert until the taint lands (NoSchedule semantics) -- safe to
    # ship now.
    tolerations = [
      {
        key      = "CriticalAddonsOnly"
        operator = "Equal"
        value    = "true"
        effect   = "NoSchedule"
      }
    ]
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
      cluster_name    = module.eks.cluster_name
      aws_account_id  = data.aws_caller_identity.current.account_id
      aws_region      = var.aws_region
      route53_zone_id = local.route53_zone_id

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
      authentik_hostname              = var.authentik_hostname
      authentik_saml_enabled          = tostring(var.authentik_saml_enabled)
      authentik_saml_idp_metadata_b64 = base64encode(local.authentik_saml_idp_metadata)
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

# ExternalSecret syncing the Authentik-issued OIDC client_secret for ArgoCD
# from the shared percona-ci-platform/authentik/config bundle into a
# single-key Secret (argocd-oidc, key client_secret) in the argocd
# namespace. ArgoCD's `oidc.config` references it as $argocd-oidc:client_secret.
#
# CRITICAL: the synced Secret MUST carry label
# `app.kubernetes.io/part-of: argocd` or argocd-server silently fails to
# resolve the $secret-name:key reference and OIDC login is broken with no
# clear error in the logs. This is enforced via the `template.metadata.labels`
# block below — ESO honors it on the materialized Secret.
#
# Why kubectl_manifest (not a chart wrapper or addons ApplicationSet):
# argocd-server reads $argocd-oidc:client_secret at startup. The Secret
# must exist before the helm_release upgrade rolls server pods, otherwise
# the new pods come up with OIDC silently broken until a manual rollout
# restart. depends_on = [helm_release.argocd] guarantees the argocd
# namespace exists; ESO reconciliation is fast (seconds), so by the time
# helm rolls server pods on a cm-checksum change, the Secret is in place.
# On a TF apply that touches helm values AND this resource together,
# Terraform applies the kubectl_manifest first (no helm-state diff), so
# ESO has a head-start before helm starts the rolling update.
resource "kubectl_manifest" "argocd_oidc_external_secret" {
  yaml_body = <<-YAML
    apiVersion: external-secrets.io/v1
    kind: ExternalSecret
    metadata:
      name: argocd-oidc
      namespace: argocd
    spec:
      refreshInterval: 1h
      secretStoreRef:
        kind: ClusterSecretStore
        name: aws-secrets-manager
      target:
        name: argocd-oidc
        creationPolicy: Owner
        template:
          metadata:
            labels:
              app.kubernetes.io/part-of: argocd
      data:
        - secretKey: client_secret
          remoteRef:
            key: percona-ci-platform/authentik/config
            property: AUTHENTIK_OIDC_ARGOCD_CLIENT_SECRET
  YAML

  depends_on = [helm_release.argocd]
}
