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
    @echo "percona-cd-platform — run a task with: just <recipe>"
    @echo ""
    @echo "Check (no AWS creds, mirrors CI):"
    @echo "  just ci              # lint + validate"
    @echo ""
    @echo "AWS recipes (export AWS_PROFILE=percona-dev-admin first):"
    @echo "  just tf-plan         # plan -> terraform/tfplan"
    @echo "  just tf-apply        # apply the saved tfplan"
    @echo "  just ssh ps80        # SSM shell to a Jenkins master"
    @echo "  just kubeconfig      # write kubeconfig for the cluster"
    @echo ""
    @just --list --list-heading $'Full recipe list:\n'

ci: lint validate
    @echo "ci passed"

lint: tf-fmt-check tf-conventions tf-trivy yaml-lint actionlint zizmor

validate: tf-validate manifest-validate helm-render clouds-render-check arch-pins-check bootstrap-priorityclass-check lambda-test

# Fail-closed arch-pin gate: no committed cluster manifest pins
# kubernetes.io/arch to a non-arm64 arch (would strand Pending on the
# all-Graviton fleet). Credential-free; the chart-default blind spot is covered
# by the `check-arch-readiness` preflight (see scripts/check_arch_pins.py).
arch-pins-check:
    uv run --with pyyaml python3 scripts/check_arch_pins.py

# Fail-closed bootstrap gate: the Terraform bootstrap copy of the
# platform-system-critical PriorityClass (terraform/argocd.tf) must stay
# identical to the GitOps-owned definition (resources/addons/priorityclasses/).
# Credential-free.
bootstrap-priorityclass-check:
    uv run --with pyyaml python3 scripts/check_bootstrap_priorityclass.py

# Arch-migration PREFLIGHT (creds): before flipping a NodePool/MNG to a new
# arch, assert every workload image publishes the target arch AND no PodSpec
# pins the wrong arch. Run it, not `just ci`, before the cutover.
#   just check-arch-readiness          # target arm64 (default)
#   just check-arch-readiness amd64
check-arch-readiness *ARGS: _require-aws-profile
    scripts/check-arch-readiness.sh {{ARGS}}

# NO-GO gate for an MNG arch flip: read the saved tfplan and assert any replaced
# node group is create-before-destroy (safe), not destroy-then-create. Run after
# `just tf-plan`. See docs/runbooks/mng-label-taint-changes.md.
tf-ng-replacement-check:
    #!/usr/bin/env bash
    set -euo pipefail
    # tofu -chdir=terraform resolves the plan path relative to terraform/.
    [[ -f "terraform/tfplan" ]] || { echo "no saved plan at terraform/tfplan; run just tf-plan first" >&2; exit 1; }
    bad="$(tofu -chdir=terraform show -json tfplan \
      | jq -r '.resource_changes[]
          | select(.type=="aws_eks_node_group")
          | select(.change.actions==["delete","create"])
          | .address')"
    if [[ -n "$bad" ]]; then
      echo "NO-GO: node group(s) replaced destroy-then-create (drops the group before its replacement):" >&2
      echo "$bad" | sed 's/^/  - /' >&2
      exit 1
    fi
    echo "tf-ng-replacement-check OK: any node-group replacement is create-before-destroy."

# ---------- cleanup lambdas ----------
# Mirrors the `cleanup-lambda tests` CI job (moto, credential-free). Runtime
# pin matches terraform/locals.tf cleanup_lambda_runtime.
lambda-test:
    uv run --python 3.14 --with-requirements terraform/lambdas/tests/requirements.txt \
      python -m pytest terraform/lambdas/tests

# Tail a reaper's dry-run/real decisions. Usage: just lambda-logs ec2-cleanup [since]
lambda-logs name since="1h": _require-aws-profile
    aws logs tail /aws/lambda/percona-ci-platform-{{name}} --since {{since}} --format short

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

# Conventions gate for terraform/ (Owner banners, no copyright / CLAUDE.md /
# ticket-ID comments) — rules in terraform/CLAUDE.md. Credential-free.
# Run via `uv run` for a consistent interpreter with the other script gates;
# the script itself is stdlib-only (no --with deps).
tf-conventions:
    uv run python3 scripts/check_conventions.py

# The real validate gate (tofu). The pre-commit terraform_validate hook
# shells the `terraform` binary instead and may fail on an old local
# install — see .pre-commit-config.yaml.
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
# (arm) EC2-fleet siblings. The -target list is DERIVED from the
# `module "<x>"` blocks in terraform/master-*.tf, so a newly migrated master
# joins the sweep automatically and can never be silently excluded (the
# hand-maintained list missed all four masters of one migration wave).
# There is intentionally NO tf-apply-masters: apply the full saved plan via
# `just tf-plan` + `just tf-apply` after review.
tf-plan-masters: _require-aws-profile
    #!/usr/bin/env bash
    set -euo pipefail
    targets=()
    for m in $(grep -hoE '^module "[a-z0-9_]+"' terraform/master-*.tf | cut -d'"' -f2 | sort); do
      targets+=("-target=module.${m}")
    done
    [ "${#targets[@]}" -gt 0 ] || { echo "ERROR: no module blocks found in terraform/master-*.tf" >&2; exit 1; }
    echo "planning ${#targets[@]} master/fleet modules"
    tofu -chdir=terraform plan "${targets[@]}"

# ---------- operator allowlists (SSM-backed) ----------
# Source of truth for the operator allowlists (eks-api, master-ssh).
# Values live only in SSM Parameter Store (CloudTrail-audited), never in git.
# A put changes nothing live until `just tf-plan && just tf-apply`.
# Ops: docs/runbooks/eks-api-access.md. Decision: docs/adr/0036.
allowlist-show: _require-aws-profile
    #!/usr/bin/env bash
    set -euo pipefail
    for p in eks-api master-ssh; do
      echo "== /{{cluster}}/allowlist/${p}"
      aws ssm get-parameter --name "/{{cluster}}/allowlist/${p}" \
        --region "${AWS_REGION:-us-east-1}" \
        --query Parameter.Value --output text | tr ',' '\n'
    done

# Usage: just allowlist-set eks-api "198.51.100.5/32,203.0.113.0/24"
# The value is the FULL comma-separated list, not a delta. Operator input is
# bound ONCE via quote() into shell variables before any other use; embedding
# quote() inside a double-quoted string would let $(...) re-evaluate, so only
# the two assignments below may interpolate the operands (docs/adr/0036).
allowlist-set name cidrs: _require-aws-profile
    #!/usr/bin/env bash
    set -euo pipefail
    name={{quote(name)}}
    cidrs={{quote(cidrs)}}
    case "$name" in eks-api|master-ssh) ;; *) printf 'unknown allowlist %s (expected: eks-api, master-ssh)\n' "$name" >&2; exit 2 ;; esac
    param="/{{cluster}}/allowlist/$name"
    echo "current:"
    aws ssm get-parameter --name "$param" --region "${AWS_REGION:-us-east-1}" \
      --query Parameter.Value --output text | tr ',' '\n'
    echo "new:"
    tr ',' '\n' <<< "$cidrs"
    read -r -p "overwrite $param? [y/N] " a
    [ "$a" = "y" ]
    aws ssm put-parameter --name "$param" --type StringList \
      --value "$cidrs" --overwrite --region "${AWS_REGION:-us-east-1}"
    echo "saved. Apply it: just tf-plan && just tf-apply"

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
      -ignore-filename-pattern '.*/clouds-catalog/.*' \
      -schema-location default \
      -schema-location "$URL" \
      argocd-bootstrap/ resources/

# ---------- jenkins controller chart ----------
# Render the per-instance Jenkins controller chart and assert the values reach
# the `jenkins` subchart (image / Retain PVC / ALB group / ClusterIP agent
# Service / node pool). Mirrors the .github/workflows/ci.yml `helm` job; uses a pinned,
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
    zizmor --quiet .github/workflows/ .github/actions/

# ---------- helpers ----------
check-versions:
    uv run --with pyyaml --with python-hcl2 python3 scripts/check_versions.py

# Gated runbook automations (docs/runbooks/common-operations.md, lands with
# the docs PR). `just runbook` lists subcommands: automated ones enforce
# their gates mechanically, guided ones confirm each step before running it.
runbook *ARGS:
    uv run --no-project python3 scripts/runbook.py {{ARGS}}

# Verify every active master (k8s / tf-managed / cf-managed) is shipping metrics
# to Mimir via Alloy. Masters enumerated dynamically from repo source-of-truth;
# per-master freshness read from the in-cluster Mimir query-frontend. Read-only.
# Pass-through args: --max-age <s>, --json.
check-master-alloy *ARGS:
    uv run --no-project python3 scripts/check-master-alloy-mimir.py {{ARGS}}

# Drift gate (ADR 0029): assert each master's committed JCasC clouds configScript
# is in sync with the shared catalog (resources/jenkins/clouds-catalog). Credential-free.
clouds-render-check:
    uv run --with pyyaml python3 scripts/render-clouds.py check ps3

# Bootstrap S3 state bucket (one-time, manual on first apply)
bootstrap-state:
    @echo "State bucket is pre-created."
    @echo "  S3:     s3://{{state_bucket}}"
    @echo "  Region: ${AWS_REGION:-us-east-1}"
    @echo "See docs/runbooks/bootstrap-state.md for the recreate-from-zero recipe."

# Fresh-cluster (DR / greenfield) bootstrap. An empty cluster needs TWO
# applies: the first cannot create the ArgoCD OIDC ExternalSecret (its CRD
# arrives only after ArgoCD syncs the external-secrets addon), so phase 1 is
# EXPECTED to exit non-zero on exactly that resource. The recipe then waits
# for the CRD, re-plans, applies again (must succeed, fail-closed), and
# restarts argocd-server so it picks up the late-arriving OIDC secret.
# Steady-state clusters: use plain `just tf-plan` + `just tf-apply`; a clean
# phase-1 apply exits here early for exactly that reason.
bootstrap-dr: _require-aws-profile
    #!/usr/bin/env bash
    set -euo pipefail
    phase1_log=$(mktemp)
    trap 'rm -f "${phase1_log}"' EXIT
    just tf-plan
    echo "== phase 1: apply (an ExternalSecret error is EXPECTED on an empty cluster) =="
    phase1_rc=0
    just tf-apply 2>&1 | tee "${phase1_log}" || phase1_rc=$?
    if ((phase1_rc == 0)); then
      echo "== single apply succeeded (cluster was not empty); bootstrap complete =="
      exit 0
    fi
    if ! grep -q 'argocd_oidc_external_secret' "${phase1_log}"; then
      echo "== phase 1 failed on something OTHER than the expected ExternalSecret; aborting ==" >&2
      exit "${phase1_rc}"
    fi
    echo "== phase 1 exited ${phase1_rc} on the expected ExternalSecret; waiting for the CRD to arrive via GitOps =="
    just kubeconfig
    kubectl --context {{cluster}} wait --for=create \
      crd/externalsecrets.external-secrets.io --timeout=15m
    kubectl --context {{cluster}} wait --for=condition=Established \
      crd/externalsecrets.external-secrets.io --timeout=2m
    echo "== phase 2: re-plan + apply (must succeed) =="
    just tf-plan
    just tf-apply
    echo "== waiting for ESO to materialize the OIDC secret, then restarting argocd-server =="
    kubectl --context {{cluster}} -n argocd wait --for=create secret/argocd-oidc --timeout=5m
    kubectl --context {{cluster}} -n argocd rollout restart deployment argocd-server
    echo "== bootstrap complete =="

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

# ---------- jenkins master shell (SSM) ----------
# The master hostnames resolve to the shared jenkins-masters ALB (HTTPS only),
# so plain `ssh <inst>.cd.percona.com` no longer reaches an EKS-fronted master.
# These recipes resolve the master instance by its billing tag in its home
# region and go through SSM (no inbound :22 needed). The interactive session
# needs the session-manager-plugin installed locally. pmm keeps the legacy
# jenkins-pmm-amzn2 tag; ps3 is the in-cluster controller (kubectl exec).
_ssm-resolve inst:
    #!/usr/bin/env bash
    set -euo pipefail
    case "{{inst}}" in
      pmm)   region=us-east-2;    tag=jenkins-pmm-amzn2 ;;
      psmdb) region=us-west-2;    tag=jenkins-psmdb ;;
      ps80)  region=us-west-2;    tag=jenkins-ps80 ;;
      pxb)   region=us-west-2;    tag=jenkins-pxb ;;
      pxc)   region=us-west-1;    tag=jenkins-pxc ;;
      ps57)  region=eu-central-1; tag=jenkins-ps57 ;;
      ps80-upgraded) region=us-west-2;    tag=jenkins-ps80-upgraded ;;
      pg)    region=eu-central-1; tag=jenkins-pg ;;
      rel)   region=eu-west-1;    tag=jenkins-rel ;;
      cloud) region=eu-west-1;    tag=jenkins-cloud ;;
      ps3)   echo "ps3 is in-cluster: use just ssh ps3 (kubectl exec into jenkins-ps3-k8s-0)" >&2; exit 2 ;;
      *)     echo "unknown instance '{{inst}}'" >&2; exit 2 ;;
    esac
    id=$(aws ec2 describe-instances --region "$region" \
      --filters "Name=tag:iit-billing-tag,Values=$tag" "Name=instance-state-name,Values=running" \
      --query 'Reservations[].Instances[].InstanceId' --output text)
    [ -n "$id" ] && [ "$(wc -w <<< "$id")" -eq 1 ] \
      || { echo "expected exactly one running '$tag' instance in $region, got: '${id:-none}'" >&2; exit 1; }
    echo "$region $id"

# Shell entrypoint. `just ssh` discovers the reachable masters live from AWS
# (running instances carrying a jenkins-* billing tag, worker tags filtered
# out); `just ssh <inst>` opens the SSM session (alias for `just ssm`).
ssh inst="": _require-aws-profile
    #!/usr/bin/env bash
    set -euo pipefail
    if [ -n "{{inst}}" ]; then exec just ssm {{inst}}; fi
    tmp=$(mktemp -d); trap 'rm -rf "$tmp"' EXIT
    regions=(us-east-2 us-west-1 us-west-2 eu-west-1 eu-central-1)
    pids=()
    for region in "${regions[@]}"; do
      ( aws ec2 describe-instances --region "$region" \
          --filters 'Name=tag:iit-billing-tag,Values=jenkins-*' 'Name=instance-state-name,Values=running' \
          --query 'Reservations[].Instances[].[Tags[?Key==`iit-billing-tag`]|[0].Value,InstanceId,PrivateIpAddress,PublicIpAddress]' \
          --output text | awk -v r="$region" '$1 !~ /-(worker|slave)/ {
            inst=$1; sub(/^jenkins-/,"",inst); sub(/-amzn2$/,"",inst);
            print inst, r, $2, $3, ($4=="None"?"-":$4)
          }' > "$tmp/$region" ) &
      pids+=($!)
    done
    fail=0
    for i in "${!pids[@]}"; do
      wait "${pids[$i]}" || { echo "WARNING: ${regions[$i]} query failed; listing is incomplete" >&2; fail=1; }
    done
    { echo "INSTANCE REGION INSTANCE-ID PRIVATE-IP PUBLIC-IP"
      sort "$tmp"/*
      echo "ps3 us-east-1 in-cluster:jenkins-ps3-k8s-0 - -"
    } | column -t
    echo >&2
    echo "usage: just ssh <instance> | just ssm-run <instance> '<cmd>'  (ps3: kubectl exec)" >&2
    [ "$fail" -eq 0 ]

# Interactive shell on a master. EC2 masters go through SSM; in-cluster (k8s)
# masters exec into the controller pod. Usage: just ssm pmm | just ssm ps3
ssm inst: _require-aws-profile
    #!/usr/bin/env bash
    set -euo pipefail
    case "{{inst}}" in
      ps3) echo "exec -> jenkins-ps3-k8s-0 (in-cluster)" >&2
           exec kubectl --context {{cluster}} -n jenkins-ps3-k8s exec -it jenkins-ps3-k8s-0 -c jenkins -- bash ;;
    esac
    vals="$(just _ssm-resolve {{inst}})"
    read -r region id <<< "$vals"
    echo "session -> {{inst}} ($id, $region)" >&2
    exec aws ssm start-session --target "$id" --region "$region"

# One-shot command on a master. SSM RunCommand runs it as root on EC2
# masters; kubectl exec runs it as the container user (jenkins, not root) on
# in-cluster ones. Prints status, stdout and stderr.
# Usage: just ssm-run pmm 'systemctl status jenkins | head -5'
ssm-run inst cmd: _require-aws-profile
    #!/usr/bin/env bash
    set -euo pipefail
    case "{{inst}}" in
      ps3) exec kubectl --context {{cluster}} -n jenkins-ps3-k8s exec jenkins-ps3-k8s-0 -c jenkins -- bash -c {{quote(cmd)}} ;;
    esac
    vals="$(just _ssm-resolve {{inst}})"
    read -r region id <<< "$vals"
    params=$(python3 -c 'import json,sys; print(json.dumps({"commands": [sys.argv[1]]}))' {{quote(cmd)}})
    cid=$(aws ssm send-command --region "$region" --instance-ids "$id" \
      --document-name AWS-RunShellScript --parameters "$params" \
      --query Command.CommandId --output text)
    aws ssm wait command-executed --command-id "$cid" --instance-id "$id" --region "$region" || true
    aws ssm get-command-invocation --command-id "$cid" --instance-id "$id" --region "$region" \
      --query '[Status,StandardOutputContent,StandardErrorContent]' --output text

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
    # Multi-arch (amd64+arm64): the EKS fleet is Graviton, so an amd64-only push
    # cannot schedule. A multi-arch push needs a docker-container builder (the
    # default docker driver cannot build multiple platforms in one manifest).
    docker buildx inspect cd-multiarch >/dev/null 2>&1 \
      || docker buildx create --name cd-multiarch --driver docker-container >/dev/null
    docker buildx build --builder cd-multiarch --platform linux/amd64,linux/arm64 \
      -t "${registry}/percona-cd/{{name}}:{{tag}}" \
      --push "images/{{name}}"
    echo "pushed multi-arch ${registry}/percona-cd/{{name}}:{{tag}}"

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
