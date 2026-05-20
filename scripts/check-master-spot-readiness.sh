#!/usr/bin/env bash
# check-master-spot-readiness.sh - read-only audit of a Jenkins master's
# spot-interrupt resilience pipeline. Confirms that the master will respond
# correctly when AWS issues a spot interruption notice:
#
#   1. SpotFleet has Capacity Rebalancing enabled (proactive replacement)
#   2. Instance is running, registered with SSM, and on the latest LT version
#   3. cron daemon is active and the terminate-check cron is installed
#   4. graceful-stop.sh exists, is executable, and uses flock for inter-run
#      mutual exclusion
#   5. Jenkins JVM is up and has the rehydrate flag set (eks_observability)
#   6. api-admin Secrets Manager fetch works via IAM role from the instance
#   7. Jenkins API auth probe returns 200 from loopback
#
# Usage:
#   scripts/check-master-spot-readiness.sh                # checks ps3
#   scripts/check-master-spot-readiness.sh pxb            # checks pxb (region eu-west-2 auto-detected)
#   scripts/check-master-spot-readiness.sh i-0123456789abcdef0  # by instance id
#
# Read-only. Exits 0 if every stage is green, 1 on any FAIL, 2 on precondition.
#
# Required on PATH: aws, jq.

set -u
set -o pipefail

usage() {
  cat <<EOF
check-master-spot-readiness.sh - spot-interrupt readiness audit for a Jenkins master
Usage: $0 [options] [<inst-or-instance-id>]
  <inst>          master shortname (default: ps3)
  <instance-id>   i-... directly (skips SpotFleet lookup by shortname tag)
  --region <r>    override AWS region (default: derive from instance/profile)
  -h, --help      show this help

Environment:
  AWS_PROFILE     AWS profile (default: percona-dev-admin)
EOF
}

INST=""
REGION=""
while [[ $# -gt 0 ]]; do
  case "$1" in
    --region) REGION="$2"; shift 2 ;;
    -h|--help) usage; exit 0 ;;
    --*) printf "unknown option: %s\n" "$1" >&2; usage >&2; exit 2 ;;
    *)
      if [[ -z "$INST" ]]; then INST="$1"; shift
      else printf "unexpected positional: %s\n" "$1" >&2; exit 2; fi ;;
  esac
done
INST="${INST:-ps3}"
PROFILE="${AWS_PROFILE:-percona-dev-admin}"

# Output helpers -------------------------------------------------------------
PASS=0
FAIL=0
SKIP=0

green() { printf '\033[32m%s\033[0m' "$*"; }
red()   { printf '\033[31m%s\033[0m' "$*"; }
gray()  { printf '\033[90m%s\033[0m' "$*"; }
section() { printf "\n== %s ==\n" "$*"; }
ok()   { printf "  %s %s %s\n" "$(green PASS)" "$1" "$(gray "${2:-}")"; PASS=$((PASS+1)); }
bad()  { printf "  %s %s %s\n" "$(red FAIL)" "$1" "${2:-}"; FAIL=$((FAIL+1)); }
skip() { printf "  %s %s %s\n" "$(gray SKIP)" "$1" "$(gray "${2:-}")"; SKIP=$((SKIP+1)); }

require() {
  command -v "$1" >/dev/null 2>&1 || { printf "missing required tool: %s\n" "$1" >&2; exit 2; }
}
for t in aws jq; do require "$t"; done

# Resolve INST -> region + instance id ---------------------------------------
# Region map for masters in known regions (matches percona-jenkins skill).
# Add new masters here when they move to Terraform.
declare -A MASTER_REGION=(
  [ps3]=eu-west-1
  [rel]=eu-west-1
  [cloud]=eu-west-1
  [pxb]=us-west-2
  [pmm]=us-east-2
  [pg]=eu-central-1
  [ps57]=eu-central-1
  [pxc]=us-west-1
  [ps80]=us-west-2
  [psmdb]=us-west-2
)

if [[ "$INST" == i-* ]]; then
  INSTANCE_ID="$INST"
  if [[ -z "$REGION" ]]; then
    # Try all known regions to find the instance.
    for r in eu-west-1 us-west-2 us-east-2 eu-central-1 us-west-1; do
      if aws ec2 describe-instances --region "$r" --profile "$PROFILE" \
           --instance-ids "$INSTANCE_ID" --query 'Reservations[0].Instances[0].State.Name' \
           --output text >/dev/null 2>&1; then
        REGION="$r"; break
      fi
    done
    if [[ -z "$REGION" ]]; then
      printf "could not locate %s in any known region; pass --region\n" "$INSTANCE_ID" >&2
      exit 2
    fi
  fi
  SHORTNAME=""
else
  SHORTNAME="$INST"
  REGION="${REGION:-${MASTER_REGION[$INST]:-}}"
  if [[ -z "$REGION" ]]; then
    printf "no region for shortname '%s'; pass --region\n" "$INST" >&2
    exit 2
  fi
  # Find running instance tagged with this master shortname.
  INSTANCE_ID=$(aws ec2 describe-instances --region "$REGION" --profile "$PROFILE" \
      --filters "Name=tag:iit-billing-tag,Values=jenkins-$INST" "Name=instance-state-name,Values=running" \
      --query 'Reservations[0].Instances[0].InstanceId' --output text 2>/dev/null)
  if [[ -z "$INSTANCE_ID" || "$INSTANCE_ID" == None ]]; then
    printf "no running instance with iit-billing-tag=jenkins-%s in %s\n" "$INST" "$REGION" >&2
    exit 2
  fi
fi

printf "checking master '%s' (instance %s in %s)\n" \
  "${SHORTNAME:-<by-id>}" "$INSTANCE_ID" "$REGION"

aws_ec2() { aws ec2 --region "$REGION" --profile "$PROFILE" "$@"; }
aws_ssm() { aws ssm --region "$REGION" --profile "$PROFILE" "$@"; }

# SSM probe wrapper - run a shell command on the instance, return stdout. Sets
# $SSM_STATUS to Success/Failed/TimedOut and $SSM_STDOUT/$SSM_STDERR.
SSM_STATUS=""; SSM_STDOUT=""; SSM_STDERR=""
ssm_run() {
  local cmd="$1" cid out
  cid=$(aws_ssm send-command --instance-ids "$INSTANCE_ID" \
        --document-name AWS-RunShellScript \
        --parameters "commands=[\"${cmd//\"/\\\"}\"]" \
        --query Command.CommandId --output text 2>/dev/null) || return 1
  for _ in $(seq 1 30); do
    sleep 2
    out=$(aws_ssm get-command-invocation --command-id "$cid" --instance-id "$INSTANCE_ID" \
          --query '{S:Status,O:StandardOutputContent,E:StandardErrorContent}' --output json 2>/dev/null) || continue
    SSM_STATUS=$(echo "$out" | jq -r .S)
    [[ "$SSM_STATUS" == "Success" || "$SSM_STATUS" == "Failed" || "$SSM_STATUS" == "TimedOut" || "$SSM_STATUS" == "Cancelled" ]] || continue
    SSM_STDOUT=$(echo "$out" | jq -r .O)
    SSM_STDERR=$(echo "$out" | jq -r .E)
    return 0
  done
  SSM_STATUS=TIMEOUT
  return 1
}

# 1. SpotFleet + CapacityRebalance check -------------------------------------
section "SpotFleet / Capacity Rebalancing"

SFR_ID=$(aws_ec2 describe-instances --instance-ids "$INSTANCE_ID" \
         --query 'Reservations[0].Instances[0].Tags[?Key==`aws:ec2spot:fleet-request-id`].Value | [0]' \
         --output text 2>/dev/null)
if [[ -z "$SFR_ID" || "$SFR_ID" == None ]]; then
  bad "instance not part of a SpotFleet" "tag aws:ec2spot:fleet-request-id missing"
else
  ok "SpotFleet membership" "$SFR_ID"
  REBAL=$(aws_ec2 describe-spot-fleet-requests --spot-fleet-request-ids "$SFR_ID" \
          --query 'SpotFleetRequestConfigs[0].SpotFleetRequestConfig.SpotMaintenanceStrategies.CapacityRebalance.ReplacementStrategy' \
          --output text 2>/dev/null)
  if [[ -z "$REBAL" || "$REBAL" == None ]]; then
    bad "Capacity Rebalancing not enabled" "no SpotMaintenanceStrategies.CapacityRebalance"
  else
    ok "Capacity Rebalancing enabled" "replacement_strategy=$REBAL"
  fi
  LT_VER=$(aws_ec2 describe-spot-fleet-requests --spot-fleet-request-ids "$SFR_ID" \
           --query 'SpotFleetRequestConfigs[0].SpotFleetRequestConfig.LaunchTemplateConfigs[0].LaunchTemplateSpecification.Version' \
           --output text 2>/dev/null)
  if [[ "$LT_VER" == "\$Latest" ]]; then
    ok "SpotFleet pinned to \$Latest" "userdata edits roll on next replacement"
  else
    bad "SpotFleet pinned to LT version $LT_VER" "should be \$Latest; userdata edits will not propagate"
  fi
fi

# 2. SSM connectivity --------------------------------------------------------
section "SSM agent"

SSM_PING=$(aws_ssm describe-instance-information \
           --filters "Key=InstanceIds,Values=$INSTANCE_ID" \
           --query 'InstanceInformationList[0].PingStatus' --output text 2>/dev/null)
if [[ "$SSM_PING" == "Online" ]]; then
  ok "SSM ping" "Online"
else
  bad "SSM ping" "status=$SSM_PING (cannot proceed with in-instance checks)"
  printf "\n== Summary ==\n  %s pass, %s fail, %s skip\n" "$PASS" "$FAIL" "$SKIP"
  exit 1
fi

# 3. cloud-init done ---------------------------------------------------------
section "cloud-init"

if ssm_run 'cloud-init status --long 2>&1 | head -2'; then
  if echo "$SSM_STDOUT" | grep -q 'status: done'; then
    ok "cloud-init status" "done"
  else
    bad "cloud-init status" "$(echo "$SSM_STDOUT" | head -1)"
  fi
else
  bad "cloud-init probe failed" "$SSM_STATUS"
fi

# 4. cron daemon + cron file -------------------------------------------------
section "cron / spot-interrupt watcher"

if ssm_run 'systemctl is-active crond; systemctl is-enabled crond'; then
  cron_active=$(echo "$SSM_STDOUT" | sed -n 1p)
  cron_enabled=$(echo "$SSM_STDOUT" | sed -n 2p)
  if [[ "$cron_active" == "active" && "$cron_enabled" == "enabled" ]]; then
    ok "crond.service" "active + enabled"
  else
    bad "crond.service" "active=$cron_active enabled=$cron_enabled"
  fi
fi

if ssm_run 'test -f /etc/cron.d/terminate-check && cat /etc/cron.d/terminate-check'; then
  if echo "$SSM_STDOUT" | grep -q '/usr/local/bin/jenkins-graceful-stop.sh'; then
    ok "/etc/cron.d/terminate-check" "calls jenkins-graceful-stop.sh"
  else
    bad "/etc/cron.d/terminate-check" "content does not reference jenkins-graceful-stop.sh"
  fi
else
  bad "/etc/cron.d/terminate-check" "file missing"
fi

# Recent cron firings (proves daemon is parsing the file).
if ssm_run 'journalctl -u crond --since "5 min ago" --no-pager 2>/dev/null | grep -c terminate-check'; then
  fires=$(echo "$SSM_STDOUT" | tr -d '\n')
  if [[ "${fires:-0}" -gt 0 ]]; then
    ok "cron recent firings" "${fires} hits of terminate-check in last 5min"
  else
    skip "cron recent firings" "no terminate-check hits in last 5min (cron may have started < 1min ago)"
  fi
fi

# 5. graceful-stop.sh --------------------------------------------------------
section "graceful-stop.sh"

if ssm_run 'test -x /usr/local/bin/jenkins-graceful-stop.sh && cat /usr/local/bin/jenkins-graceful-stop.sh'; then
  ok "jenkins-graceful-stop.sh exists + executable"
  if echo "$SSM_STDOUT" | grep -q '^flock\b\|^[[:space:]]*flock\b\|^exec 9>'; then
    ok "graceful-stop has flock guard" "no concurrent-drain pile-ups"
  else
    bad "graceful-stop missing flock guard" "concurrent cron firings will race after safeExit"
  fi
  if echo "$SSM_STDOUT" | grep -q 'X-aws-ec2-metadata-token'; then
    ok "graceful-stop uses IMDSv2" "tokens negotiated before metadata reads"
  else
    bad "graceful-stop uses IMDSv1" "LT requires IMDSv2; script will fail on token-required IMDS"
  fi
else
  bad "jenkins-graceful-stop.sh" "missing or not executable"
fi

if ssm_run 'command -v jq && jq --version 2>&1 | head -1'; then
  ok "jq installed" "graceful-stop dependency"
else
  bad "jq missing" "graceful-stop busyExecutors poll will fail"
fi

# 6. jenkins.service + JVM args ---------------------------------------------
section "jenkins.service + JVM args"

if ssm_run 'systemctl is-active jenkins'; then
  if [[ "$(echo "$SSM_STDOUT" | tr -d '[:space:]')" == "active" ]]; then
    ok "jenkins.service" "active"
  else
    bad "jenkins.service" "state=$SSM_STDOUT"
  fi
fi

if ssm_run "ps -ef | grep jenkins | grep -v grep | grep -oE -- '-Dhetzner.rehydrate[^ ]+' || echo MISSING"; then
  if echo "$SSM_STDOUT" | grep -q rehydrate.enabled=true; then
    ok "rehydrate flag in JVM args" "$(echo "$SSM_STDOUT" | tr '\n' ' ' | head -c 100)"
  else
    skip "rehydrate flag" "not set; only required for eks_observability profile"
  fi
fi

# 7. Secrets Manager + api-admin auth probe ---------------------------------
section "api-admin SM + Jenkins auth"

if ssm_run 'TOKEN=$(aws secretsmanager get-secret-value --region '"$REGION"' --secret-id '"$INST"'.cd/jenkins/admin-api-token --query SecretString --output text 2>&1); echo "sm-exit=$?"; echo "token-len=${#TOKEN}"'; then
  if echo "$SSM_STDOUT" | grep -q 'sm-exit=0'; then
    tok_len=$(echo "$SSM_STDOUT" | grep token-len | head -1)
    ok "Secrets Manager fetch" "via instance IAM role, $tok_len"
  else
    bad "Secrets Manager fetch failed" "$(echo "$SSM_STDOUT" | head -3 | tr '\n' '|')"
  fi
fi

if ssm_run 'TOKEN=$(aws secretsmanager get-secret-value --region '"$REGION"' --secret-id '"$INST"'.cd/jenkins/admin-api-token --query SecretString --output text 2>/dev/null); curl -fsS -u "api-admin:$TOKEN" -o /dev/null -w "http=%{http_code}\n" http://127.0.0.1:8080/whoAmI/api/json 2>&1 | head -1'; then
  if echo "$SSM_STDOUT" | grep -q 'http=200'; then
    ok "Jenkins api-admin auth probe" "loopback returns 200"
  else
    bad "Jenkins api-admin auth probe" "$(echo "$SSM_STDOUT" | head -1)"
  fi
fi

if ssm_run 'TOKEN=$(aws secretsmanager get-secret-value --region '"$REGION"' --secret-id '"$INST"'.cd/jenkins/admin-api-token --query SecretString --output text 2>/dev/null); curl -fsS -u "api-admin:$TOKEN" "http://127.0.0.1:8080/computer/api/json?tree=busyExecutors" 2>&1 | jq -r .busyExecutors'; then
  busy="$(echo "$SSM_STDOUT" | tr -d '[:space:]')"
  if [[ "$busy" =~ ^[0-9]+$ ]]; then
    ok "busyExecutors endpoint" "drain prerequisite OK, current=$busy"
  else
    bad "busyExecutors endpoint" "got '$busy'"
  fi
fi

# Summary --------------------------------------------------------------------
section "Summary"
TOTAL=$((PASS + FAIL + SKIP))
printf "  %s pass, %s fail, %s skip (of %s)\n" \
  "$(green "$PASS")" "$(red "$FAIL")" "$(gray "$SKIP")" "$TOTAL"
if [[ "$FAIL" -gt 0 ]]; then
  printf "  -> instance will NOT respond correctly to a spot interrupt\n"
  exit 1
fi
printf "  -> instance is ready to respond to a spot interrupt\n"
exit 0
