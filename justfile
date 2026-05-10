set shell := ["bash", "-eu", "-o", "pipefail", "-c"]

# ---------- toolchain pins (mirror .pre-commit-config.yaml + .github/workflows/ci.yml) ----------
tofu_version    := "1.11.6"
tflint_version  := "0.62.0"
actionlint_ver  := "1.7.12"
zizmor_ver      := "1.24.1"
kubeconform_ver := "0.7.0"
trivy_ver       := "0.70.0"
yamllint_ver    := "1.38.0"
just_ver        := "1.50.0"

# ---------- AWS context ----------
# AWS_PROFILE: set in your shell to override; defaults to percona-dev-admin
# (the only profile with the IAM scope to manage this cluster's resources).
# Recipes that touch AWS (tf-plan, tf-apply, tf-destroy, tf-init-backend)
# inject this so a fresh shell `just tf-apply` works without manual export.
aws_profile := env_var_or_default("AWS_PROFILE", "percona-dev-admin")
aws_region  := env_var_or_default("AWS_REGION", "us-east-1")
cluster     := "percona-ci-platform"

# ---------- top-level ----------
default: help

help:
    @just --list

ci: lint validate
    @echo "✅ ci passed"

lint: tf-fmt-check tf-trivy yaml-lint actionlint zizmor

validate: tf-validate manifest-validate

# ---------- terraform / opentofu ----------
tf-init:
    cd terraform && tofu init -backend=false -upgrade

tf-init-backend:
    cd terraform && AWS_PROFILE={{aws_profile}} tofu init -upgrade

tf-fmt:
    tofu fmt -recursive

tf-fmt-check:
    tofu fmt -recursive -check -diff

tf-validate: tf-init
    cd terraform && tofu validate

# tflint disabled: its terraform plugin (v0.14.x) doesn't understand OpenTofu 1.8+
# early-eval syntax we use for module pins (versions.tf, D11). Re-enable when supported.
#tf-lint:
#    cd terraform && tflint --init && tflint --recursive --format compact

tf-trivy:
    trivy config --quiet --severity HIGH,CRITICAL --exit-code 1 \
      --skip-dirs terraform/.terraform \
      --skip-files terraform/tfplan \
      --ignorefile .trivyignore terraform/

tf-plan:
    cd terraform && AWS_PROFILE={{aws_profile}} tofu plan -out=tfplan

tf-apply:
    cd terraform && AWS_PROFILE={{aws_profile}} tofu apply tfplan

# Fast path for routine apply: plan + apply in one shot, auto-approved.
# Use tf-plan + tf-apply when you want a separate review step.
tf-apply-now:
    cd terraform && AWS_PROFILE={{aws_profile}} tofu apply -auto-approve

tf-destroy:
    cd terraform && AWS_PROFILE={{aws_profile}} tofu destroy

# ---------- gitops / yaml ----------
yaml-lint:
    yamllint -s argocd-bootstrap/ resources/ .github/

manifest-validate:
    #!/usr/bin/env bash
    set -euo pipefail
    URL='https://raw.githubusercontent.com/datreeio/CRDs-catalog/main/{{{{.Group}}}}/{{{{.ResourceKind}}}}_{{{{.ResourceAPIVersion}}}}.json'
    kubeconform -strict -summary -ignore-missing-schemas \
      -ignore-filename-pattern '(Chart|values.*)\.yaml' \
      -ignore-filename-pattern '.*/templates/.*' \
      -schema-location default \
      -schema-location "$URL" \
      argocd-bootstrap/ resources/

# ---------- workflow security ----------
actionlint:
    actionlint -color

zizmor:
    zizmor --quiet .github/workflows/

# ---------- helpers ----------
check-versions:
    uv run --with pyyaml python3 scripts/check_versions.py

# Bootstrap S3 state bucket (one-time, manual on first apply)
bootstrap-state:
    @echo "State bucket is pre-created."
    @echo "  S3:     s3://terraform-state-storage-{{cluster}}"
    @echo "  Region: {{aws_region}}"
    @echo "See docs/runbooks/bootstrap-state.md for the recreate-from-zero recipe."

# Update local kubeconfig to talk to the cluster.
# Honours AWS_PROFILE if set; otherwise uses the SDK default chain.
kubeconfig:
    aws eks update-kubeconfig --name {{cluster}} --region {{aws_region}} --alias {{cluster}}

# Status snapshot
status:
    kubectl --context {{cluster}} get nodes -o wide
    kubectl --context {{cluster}} get pods -A
    argocd app list

# Sync everything from git (use sparingly; ArgoCD auto-syncs by default)
sync-all:
    argocd app list -o name | xargs -n1 argocd app sync

# ---------- Karpenter validation ----------
# Systematic before/during/after harness for scale-up + scale-down.
# Plan: ~/.claude/plans/hashed-shimmying-crane.md.
verify-karpenter *ARGS:
    AWS_PROFILE={{aws_profile}} scripts/verify-karpenter.sh {{ARGS}}

# ---------- observability verification ----------
# Per-master deep walk of the LGTM push pipeline (SSH-dependent stages included).
# Default master ps3; override: `just verify-obs cloud`. Pass extra args after.
verify-obs INST="ps3" *ARGS="":
    AWS_PROFILE={{aws_profile}} scripts/verify-observability.sh {{INST}} {{ARGS}}

# Fleet-wide ingest spot-check via in-cluster query-frontends (no SSH).
# `just check-ingest` defaults to a 1h window; widen with `just check-ingest 24h`.
check-ingest LOOKBACK="1h":
    AWS_PROFILE={{aws_profile}} scripts/check-master-ingest.sh --lookback {{LOOKBACK}}

# ---------- ArgoCD UI port-forward (browser) ----------
argocd-ui:
    @echo "Open https://localhost:8443 (admin / from initial-admin-secret)"
    kubectl --context {{cluster}} -n argocd port-forward svc/argocd-server 8443:443

argocd-password:
    kubectl --context {{cluster}} -n argocd get secret argocd-initial-admin-secret \
        -o jsonpath='{.data.password}' | base64 -d && echo

# ---------- pre-commit ----------
pre-commit-install:
    pre-commit install --install-hooks

pre-commit-run:
    pre-commit run --all-files
