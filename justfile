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
helm_version      := "3.16.4"
helm_sha256_amd64 := "fc307327959aa38ed8f9f7e66d45492bb022a66c3e5da6063958254b9767d179"
helm_sha256_arm64 := "d3f8f15b3d9ec8c8678fbf3280c3e5902efabe5912e2f9fcf29107efbc8ead69"

# ---------- AWS context ----------
# AWS_PROFILE / AWS_REGION come from the operator's environment. They are NEVER
# interpolated into a recipe's shell source (interpolating a value like $(...)
# would let it execute); recipes reference the inherited $AWS_PROFILE /
# ${AWS_REGION} directly, and `_require-aws-profile` asserts AWS_PROFILE is set.
# Credential-free recipes (ci, lint, validate, tf-init, tf-fmt) never touch it,
# so they parse and run with no profile set — exactly how .github/workflows/ci.yml
# runs them (no AWS creds). providers.tf falls through to the SDK default chain
# when var.aws_profile == "".
cluster      := "percona-ci-platform"
state_bucket := "terraform-state-storage-" + cluster

# ---------- top-level ----------
default: help

help:
    @just --list

ci: lint validate
    @echo "✅ ci passed"

lint: tf-fmt-check tf-trivy yaml-lint actionlint zizmor

validate: tf-validate manifest-validate helm-render

# ---------- internal guards ----------
# Fail loudly (at runtime, never parse time) if AWS_PROFILE is not exported.
# Reads the env var directly (no {{...}} interpolation) so its value is never
# evaluated as shell source. A dependency of every AWS-touching recipe.
_require-aws-profile:
    @: "${AWS_PROFILE:?AWS_PROFILE must be exported (e.g. export AWS_PROFILE=percona-dev-admin); do NOT set aws_profile in local.auto.tfvars}"

# ---------- terraform / opentofu ----------
# Offline init — no backend, no credentials, no -upgrade (matches CI's
# `tofu init -backend=false`, so `just ci` does not rewrite .terraform.lock.hcl).
tf-init:
    tofu -chdir=terraform init -backend=false

# Real backend init (reads the inherited AWS_PROFILE); -upgrade is intentional here.
tf-init-backend: _require-aws-profile
    tofu -chdir=terraform init -upgrade

# fmt stays at repo root (recurses into terraform/ and every module).
tf-fmt:
    tofu fmt -recursive

tf-fmt-check:
    tofu fmt -recursive -check -diff

tf-validate: tf-init
    tofu -chdir=terraform validate

# tflint disabled: its terraform plugin (v0.14.x) doesn't understand OpenTofu 1.8+
# early-eval syntax we use for module pins (versions.tf, D11). Re-enable when supported.
#tf-lint:
#    tofu -chdir=terraform tflint --init && tflint --recursive --format compact

tf-trivy:
    trivy config --quiet --severity HIGH,CRITICAL --exit-code 1 \
      --skip-dirs terraform/.terraform \
      --skip-files terraform/tfplan \
      --ignorefile .trivyignore terraform/

tf-plan: _require-aws-profile
    tofu -chdir=terraform plan -out=tfplan

# Applies the SAVED plan from `just tf-plan`. NEVER auto-approve. Re-run tf-plan
# first if terraform/tfplan is stale; tofu rejects an out-of-date saved plan.
tf-apply: _require-aws-profile
    tofu -chdir=terraform apply tfplan

tf-destroy: _require-aws-profile
    tofu -chdir=terraform destroy

# Back up live state to a local, gitignored snapshot before any risky apply.
# *.tfstate is already gitignored; .state-backups/ stays out of git the same way.
tf-state-backup: _require-aws-profile
    #!/usr/bin/env bash
    set -euo pipefail
    mkdir -p terraform/.state-backups
    ts="$(date -u +%Y%m%dT%H%M%SZ)"
    out="terraform/.state-backups/terraform.${ts}.tfstate"
    tofu -chdir=terraform state pull > "$out"
    echo "state backed up to $out"

# Read-only GATE: assert the state bucket has S3 Object Versioning enabled (the
# only recovery path if a state write goes wrong). Prints the status and EXITS
# NON-ZERO if it is not "Enabled" — STOP and enable it before any apply.
tf-state-versioning-check: _require-aws-profile
    #!/usr/bin/env bash
    set -euo pipefail
    status="$(aws s3api get-bucket-versioning \
      --bucket {{state_bucket}} \
      --region "${AWS_REGION:-us-east-1}" \
      --query Status --output text)"
    echo "state bucket versioning: ${status}"
    [ "${status}" = "Enabled" ] || { echo "ERROR: state bucket versioning is '${status}', expected 'Enabled' — STOP and enable it before any apply." >&2; exit 1; }

# PLAN-ONLY, read-only review of the Jenkins master modules and their Graviton
# (arm) EC2-fleet siblings. Enumerates each *_arm_fleet sibling explicitly —
# omitting them would silently exclude the Graviton fleets from the plan.
# There is intentionally NO tf-apply-masters: apply the full saved plan via
# `just tf-plan` + `just tf-apply` after review.
tf-plan-masters: _require-aws-profile
    tofu -chdir=terraform plan \
      -target=module.ps3   -target=module.ps3_arm_fleet \
      -target=module.ps57  -target=module.ps57_arm_fleet \
      -target=module.ps80  -target=module.ps80_arm_fleet \
      -target=module.pxb   -target=module.pxb_arm_fleet \
      -target=module.pxc   -target=module.pxc_arm_fleet

# ---------- gitops / yaml ----------
yaml-lint:
    yamllint -s argocd-bootstrap/ resources/ .github/

manifest-validate:
    #!/usr/bin/env bash
    set -euo pipefail
    URL='https://raw.githubusercontent.com/datreeio/CRDs-catalog/main/{{{{.Group}}/{{{{.ResourceKind}}_{{{{.ResourceAPIVersion}}.json'
    kubeconform -strict -summary -ignore-missing-schemas \
      -ignore-filename-pattern '(Chart|values.*)\.yaml' \
      -ignore-filename-pattern '.*/templates/.*' \
      -ignore-filename-pattern '.*/files/.*' \
      -ignore-filename-pattern '.*/dashboards/.*\.json' \
      -schema-location default \
      -schema-location "$URL" \
      argocd-bootstrap/ resources/

# ---------- jenkins controller chart ----------
# Render the per-instance Jenkins controller chart and assert the values reach
# the `jenkins` subchart (image / Retain PVC / ALB group / JNLP listener / node
# pool). Mirrors the .github/workflows/ci.yml `helm` job; uses a pinned,
# sha-verified helm cached under .cache/.
helm-render: _helm
    PATH="$(pwd)/.cache/helm:$PATH" scripts/jenkins-chart-render-check.sh

# Download + sha256-verify the pinned helm into .cache/helm (idempotent,
# arch-detected). Pins mirror .github/workflows/ci.yml.
_helm:
    #!/usr/bin/env bash
    set -euo pipefail
    dest=".cache/helm"
    if [ -x "$dest/helm" ] && "$dest/helm" version --short 2>/dev/null | grep -q "v{{helm_version}}"; then exit 0; fi
    case "$(uname -m)" in
      x86_64|amd64)  arch=amd64; sha="{{helm_sha256_amd64}}" ;;
      aarch64|arm64) arch=arm64; sha="{{helm_sha256_arm64}}" ;;
      *) echo "unsupported arch: $(uname -m)" >&2; exit 1 ;;
    esac
    mkdir -p "$dest"
    tgz="$(mktemp)"
    curl -fsSL -o "$tgz" "https://get.helm.sh/helm-v{{helm_version}}-linux-${arch}.tar.gz"
    echo "${sha}  ${tgz}" | sha256sum -c -
    tar -xzf "$tgz" -C "$dest" --strip-components=1 "linux-${arch}/helm"
    rm -f "$tgz"
    "$dest/helm" version --short

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
    @echo "  S3:     s3://{{state_bucket}}"
    @echo "  Region: ${AWS_REGION:-us-east-1}"
    @echo "See docs/runbooks/bootstrap-state.md for the recreate-from-zero recipe."

# Update local kubeconfig to talk to the cluster.
# Honours AWS_PROFILE if set; otherwise uses the SDK default chain.
kubeconfig:
    aws eks update-kubeconfig --name {{cluster}} --region "${AWS_REGION:-us-east-1}" --alias {{cluster}}

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
verify-karpenter *ARGS: _require-aws-profile
    scripts/verify-karpenter.sh {{ARGS}}

# ---------- ArgoCD UI port-forward (browser) ----------
argocd-ui:
    @echo "Open https://localhost:8443 (admin / from initial-admin-secret)"
    kubectl --context {{cluster}} -n argocd port-forward svc/argocd-server 8443:443

argocd-password:
    kubectl --context {{cluster}} -n argocd get secret argocd-initial-admin-secret \
        -o jsonpath='{.data.password}' | base64 -d && echo

# ---------- container images (ECR, percona-cd/ namespace) ----------
# Build + push a custom addon image. The account ID is resolved at runtime
# (never committed); EKS nodes pull from this ECR via the node role.
# Build context is images/<name>; the ECR repo (percona-cd/<name>) is managed
# in terraform/ecr.tf. Usage:
#   just build-image mtr-ingest 0.1.0
#   just build-image jenkins-endpoint-reconciler 0.1.0
build-image name tag="0.1.0": _require-aws-profile
    #!/usr/bin/env bash
    set -euo pipefail
    account=$(aws sts get-caller-identity --query Account --output text)
    registry="${account}.dkr.ecr.${AWS_REGION:-us-east-1}.amazonaws.com"
    aws ecr get-login-password --region "${AWS_REGION:-us-east-1}" \
      | docker login --username AWS --password-stdin "$registry"
    docker buildx build --platform linux/amd64 \
      -t "${registry}/percona-cd/{{name}}:{{tag}}" \
      --push "images/{{name}}"
    echo "pushed ${registry}/percona-cd/{{name}}:{{tag}}"

# ---------- jenkins fork plugin locks ----------
# Recorded-pin auto-bump: rewrite images/jenkins/percona-plugins.lock.json to the
# latest published `.percona.` fork releases (ec2, hetzner-cloud), sha256- and
# MANIFEST-verified. Mirrors .github/workflows/refresh-fork-locks.yml, which then
# build+smoke-validates and opens a PR (no auto-merge). Local run rewrites the file
# only; commit + PR are the CI job's responsibility.
refresh-fork-locks:
    scripts/refresh-fork-locks.sh

# Report-only probe: exit 3 if a newer fork release is available, no file changes.
check-fork-locks:
    scripts/refresh-fork-locks.sh --check

# ---------- pre-commit ----------
pre-commit-install:
    pre-commit install --install-hooks

pre-commit-run:
    pre-commit run --all-files
