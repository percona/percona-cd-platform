#!/usr/bin/env bash
#
# Reconcile the engineer SSH keys on a Jenkins master from the fleet roster in
# SSM Parameter Store (docs/adr/0046).
#
# Reads {"schema":1,"engineers":[...]} from ROSTER_PARAMETER, fetches every
# slug's public key from percona.com, validates each key line, and atomically
# replaces the dedicated key file that the sshd drop-in names. Any failure keeps
# the last good file byte-identical and exits non-zero. An empty roster writes
# an empty file without contacting percona.com. Once sshd has validated AND
# reloaded the drop-in (recorded in a marker, so an interrupted run cannot
# skip it), the legacy engineer keys are pruned from the login user's
# authorized_keys one time, with a dated backup, leaving only the EC2 key pair
# that instance metadata reports.
#
# Every write, rename and permission change is checked explicitly. The helpers
# run in contexts where bash disables errexit, so set -e is a backstop here,
# never the guarantee.
#
# Environment (defaults are the production paths, tests override them):
#   ROSTER_PARAMETER   SSM parameter name (required)
#   ROSTER_REGION      region of the parameter (required)
#   KEY_URL_TEMPLATE   printf template with one %s for the slug
#   LOGIN_USER         account whose keys are managed
#   SSH_CONFIG_DIR     sshd configuration directory
#   LOGIN_HOME         home directory of LOGIN_USER
#   IMDS_BASE          instance metadata endpoint
#   LOCK_FILE          flock path
#   METRICS_DIR        node_exporter textfile directory scraped by Alloy
set -euo pipefail
export PATH="${PATH}:/usr/sbin:/sbin"

readonly ROSTER_PARAMETER="${ROSTER_PARAMETER:?ROSTER_PARAMETER is required}"
readonly ROSTER_REGION="${ROSTER_REGION:?ROSTER_REGION is required}"
readonly KEY_URL_TEMPLATE="${KEY_URL_TEMPLATE:-https://www.percona.com/get/engineer/KEY/%s.pub}"
readonly LOGIN_USER="${LOGIN_USER:-ec2-user}"
readonly SSH_CONFIG_DIR="${SSH_CONFIG_DIR:-/etc/ssh}"
readonly LOGIN_HOME="${LOGIN_HOME:-/home/${LOGIN_USER}}"
readonly IMDS_BASE="${IMDS_BASE:-http://169.254.169.254}"
readonly LOCK_FILE="${LOCK_FILE:-/run/lock/engineer-keys-sync.lock}"
readonly METRICS_DIR="${METRICS_DIR:-/var/lib/alloy/textfile}"
readonly METRICS_FILE="${METRICS_DIR}/engineer_keys_sync.prom"

readonly KEYS_DIR="${SSH_CONFIG_DIR}/authorized_keys.d"
readonly KEYS_FILE="${KEYS_DIR}/${LOGIN_USER}.engineers"
readonly DROPIN_DIR="${SSH_CONFIG_DIR}/sshd_config.d"
readonly DROPIN_FILE="${DROPIN_DIR}/40-engineer-keys.conf"
readonly DROPIN_CONTENT="AuthorizedKeysFile .ssh/authorized_keys ${KEYS_DIR}/%u.engineers"
readonly DROPIN_LIVE_MARKER="${KEYS_DIR}/.dropin-live"
readonly PRUNE_MARKER="${KEYS_DIR}/.legacy-pruned"
readonly LEGACY_FILE="${LOGIN_HOME}/.ssh/authorized_keys"
readonly SLUG_PATTERN='^[a-z0-9][a-z0-9._-]{0,63}$'
readonly KEY_TYPE_PATTERN='^(ssh-ed25519|ssh-rsa|ecdsa-sha2-nistp(256|384|521)|sk-ssh-ed25519@openssh\.com|sk-ecdsa-sha2-nistp256@openssh\.com) [A-Za-z0-9+/=]+( .*)?$'
readonly LOG_TAG="engineer-keys-sync"

# Outcome of each stage, set by the helpers and reported in the summary.
KEYS_STATE=""
DROPIN_STATE=""
PRUNE_STATE=""
ROSTER_VERSION=""
KEY_COUNT=""

# Log lines go to the journal and to stderr, so a message survives even when a
# caller captures stdout.
log() {
  local message="$*"
  if command -v logger >/dev/null 2>&1; then
    logger -t "${LOG_TAG}" -- "${message}" || :
  fi
  echo "${LOG_TAG}: ${message}" >&2
}

# Publishes the run outcome for the Alloy textfile collector, so a failing or
# silent sync raises an alert in Mimir (docs/observability.md, Engineer keys
# sync). Best effort: a metrics problem is logged and never changes the exit
# code. The last success timestamp survives failed runs.
write_metrics() {
  local success="$1"
  local now previous_success staging
  now="$(date -u +%s)"
  previous_success="$(awk '$1 == "engineer_keys_sync_last_success_timestamp_seconds" {print $2}' "${METRICS_FILE}" 2>/dev/null || :)"
  if [[ "${success}" -eq 1 ]]; then
    previous_success="${now}"
  fi
  install -d -m 0755 "${METRICS_DIR}" 2>/dev/null || { log "metrics directory unavailable"; return 0; }
  staging="$(mktemp -p "${METRICS_DIR}" 2>/dev/null)" || { log "metrics staging failed"; return 0; }
  {
    echo "# HELP engineer_keys_sync_last_run_timestamp_seconds Unix time of the last reconciler run."
    echo "# TYPE engineer_keys_sync_last_run_timestamp_seconds gauge"
    echo "engineer_keys_sync_last_run_timestamp_seconds ${now}"
    echo "# HELP engineer_keys_sync_last_run_success 1 when the last run applied the roster, 0 when it failed."
    echo "# TYPE engineer_keys_sync_last_run_success gauge"
    echo "engineer_keys_sync_last_run_success ${success}"
    if [[ -n "${previous_success}" ]]; then
      echo "# HELP engineer_keys_sync_last_success_timestamp_seconds Unix time of the last run that applied the roster."
      echo "# TYPE engineer_keys_sync_last_success_timestamp_seconds gauge"
      echo "engineer_keys_sync_last_success_timestamp_seconds ${previous_success}"
    fi
    if [[ -n "${ROSTER_VERSION}" ]]; then
      echo "# HELP engineer_keys_sync_roster_version SSM parameter version of the roster last read."
      echo "# TYPE engineer_keys_sync_roster_version gauge"
      echo "engineer_keys_sync_roster_version ${ROSTER_VERSION}"
    fi
    if [[ -n "${KEY_COUNT}" ]]; then
      echo "# HELP engineer_keys_sync_keys Public keys in the managed roster file."
      echo "# TYPE engineer_keys_sync_keys gauge"
      echo "engineer_keys_sync_keys ${KEY_COUNT}"
    fi
  } >"${staging}" || { rm -f "${staging}"; log "metrics write failed"; return 0; }
  if ! chmod 0644 "${staging}"; then
    rm -f "${staging}"
    log "metrics chmod failed"
    return 0
  fi
  if ! mv -f "${staging}" "${METRICS_FILE}"; then
    rm -f "${staging}"
    log "metrics publish failed"
  fi
}

die() {
  log "FAIL $*"
  write_metrics 0
  exit 1
}

sha256_of() {
  local path="$1"
  if [[ -f "${path}" ]]; then
    sha256sum "${path}" | awk '{print $1}'
  else
    echo "absent"
  fi
}

sha256_of_string() {
  printf '%s\n' "$1" | sha256sum | awk '{print $1}'
}

# chown only when root, so the tests can run as a normal user against a
# scratch root. Returns non-zero when the chown itself fails.
own() {
  local owner="$1"
  shift
  if [[ "${EUID}" -eq 0 ]]; then
    chown "${owner}:${owner}" "$@" || return 1
  fi
}

# Prints "VERSION=<n>" then one validated slug per line, sorted and deduplicated.
read_roster() {
  local raw
  raw="$(aws ssm get-parameter --region "${ROSTER_REGION}" --name "${ROSTER_PARAMETER}" --output json)" \
    || die "cannot read ${ROSTER_PARAMETER} in ${ROSTER_REGION}"
  ROSTER_RAW="${raw}" python3 - "${SLUG_PATTERN}" <<'PY' || die "roster parameter is not a valid schema 1 roster"
import json, os, re, sys
pattern = re.compile(sys.argv[1])
parameter = json.loads(os.environ["ROSTER_RAW"])["Parameter"]
roster = json.loads(parameter["Value"])
if roster.get("schema") != 1:
    sys.exit(2)
engineers = roster.get("engineers")
if not isinstance(engineers, list):
    sys.exit(2)
for slug in engineers:
    if not isinstance(slug, str) or not pattern.match(slug):
        sys.exit(2)
print(f"VERSION={parameter['Version']}")
for slug in sorted(set(engineers)):
    print(slug)
PY
}

# Fetches one slug's .pub, validates every line, and appends normalised
# "<type> <blob> <slug>" lines to the staging file. Prints the number of keys
# appended. Returns 1 on any problem, including a failed write.
append_slug_keys() {
  local slug="$1"
  local staging="$2"
  local url fetched line type blob key_count=0
  # shellcheck disable=SC2059  # the template is the format string by design
  url="$(printf "${KEY_URL_TEMPLATE}" "${slug}")"
  fetched="$(mktemp)" || { log "mktemp failed"; return 1; }
  if ! curl -fsS --max-time 20 --retry 2 --retry-delay 2 -o "${fetched}" "${url}"; then
    rm -f "${fetched}"
    log "fetch failed for ${slug}"
    return 1
  fi
  while IFS= read -r line || [[ -n "${line}" ]]; do
    [[ -z "${line}" ]] && continue
    if [[ ! "${line}" =~ ${KEY_TYPE_PATTERN} ]]; then
      rm -f "${fetched}"
      log "malformed key line for ${slug}"
      return 1
    fi
    if ! ssh-keygen -lf <(printf '%s\n' "${line}") >/dev/null 2>&1; then
      rm -f "${fetched}"
      log "key rejected by ssh-keygen for ${slug}"
      return 1
    fi
    read -r type blob _ <<<"${line}"
    if ! printf '%s %s %s\n' "${type}" "${blob}" "${slug}" >>"${staging}"; then
      rm -f "${fetched}"
      log "write to staging failed for ${slug}"
      return 1
    fi
    key_count=$((key_count + 1))
  done <"${fetched}"
  rm -f "${fetched}"
  if [[ "${key_count}" -eq 0 ]]; then
    log "no key served for ${slug}"
    return 1
  fi
  echo "${key_count}"
}

# Publishes the staged roster file atomically after verifying its content.
publish_keys_file() {
  local staging="$1"
  local expected_keys="$2"
  local staged_keys before after
  staged_keys="$(grep -c . "${staging}" || :)"
  if [[ "${staged_keys}" -ne "${expected_keys}" ]]; then
    log "staging holds ${staged_keys} keys, expected ${expected_keys}"
    return 1
  fi
  before="$(sha256_of "${KEYS_FILE}")"
  after="$(sha256_of "${staging}")"
  if [[ "${before}" == "${after}" ]]; then
    rm -f "${staging}"
    KEYS_STATE="unchanged"
    return 0
  fi
  chmod 0644 "${staging}" || { log "chmod on staging failed"; return 1; }
  own root "${staging}" || { log "chown on staging failed"; return 1; }
  mv -f "${staging}" "${KEYS_FILE}" || { log "rename into ${KEYS_FILE} failed"; return 1; }
  if [[ "$(sha256_of "${KEYS_FILE}")" != "${after}" ]]; then
    log "published file does not match staging"
    return 1
  fi
  KEYS_STATE="changed"
}

# Puts the previous drop-in back (or removes ours when there was none), then
# asks sshd to pick that up again. Best effort, used only on the failure path.
restore_dropin() {
  local previous="$1"
  local had_previous="$2"
  if [[ "${had_previous}" -eq 1 ]]; then
    mv -f "${previous}" "${DROPIN_FILE}" || log "could not restore the previous drop-in"
  else
    rm -f "${DROPIN_FILE}" "${previous}"
  fi
  if sshd -t; then
    systemctl reload sshd || log "sshd reload after restore failed"
  fi
}

# Installs the drop-in when missing or different, validates the whole sshd
# configuration, reloads sshd, and records the reloaded content in a marker.
# The marker is what makes the drop-in count as live: a run that dies between
# the rename and the reload leaves no marker, so the next run validates and
# reloads again even though the file on disk already looks right.
ensure_dropin() {
  local wanted current live
  wanted="$(sha256_of_string "${DROPIN_CONTENT}")"
  current="$(sha256_of "${DROPIN_FILE}")"
  live="$(cat "${DROPIN_LIVE_MARKER}" 2>/dev/null || echo absent)"
  if [[ "${current}" == "${wanted}" && "${live}" == "${wanted}" ]]; then
    DROPIN_STATE="kept"
    return 0
  fi
  local previous had_previous=0 staging
  previous="$(mktemp)" || { log "mktemp failed"; return 1; }
  if [[ -f "${DROPIN_FILE}" ]]; then
    cp -p "${DROPIN_FILE}" "${previous}" || { log "backup of the drop-in failed"; return 1; }
    had_previous=1
  fi
  staging="$(mktemp -p "${DROPIN_DIR}")" || { log "mktemp in ${DROPIN_DIR} failed"; return 1; }
  printf '%s\n' "${DROPIN_CONTENT}" >"${staging}" || { log "write of the drop-in failed"; return 1; }
  chmod 0644 "${staging}" || { log "chmod on the drop-in failed"; return 1; }
  own root "${staging}" || { log "chown on the drop-in failed"; return 1; }
  mv -f "${staging}" "${DROPIN_FILE}" || { log "rename of the drop-in failed"; return 1; }
  if ! sshd -t; then
    restore_dropin "${previous}" "${had_previous}"
    log "sshd rejected the configuration with the drop-in, previous state restored"
    return 1
  fi
  if ! systemctl reload sshd; then
    restore_dropin "${previous}" "${had_previous}"
    log "sshd reload failed, previous state restored"
    return 1
  fi
  rm -f "${previous}"
  printf '%s\n' "${wanted}" >"${DROPIN_LIVE_MARKER}" || { log "write of the live marker failed"; return 1; }
  DROPIN_STATE="reloaded"
}

# True when the reloaded drop-in is recorded AND the running configuration
# resolves AuthorizedKeysFile to exactly the two expected entries.
dropin_is_live() {
  local live wanted line first second
  wanted="$(sha256_of_string "${DROPIN_CONTENT}")"
  live="$(cat "${DROPIN_LIVE_MARKER}" 2>/dev/null || echo absent)"
  [[ "${live}" == "${wanted}" ]] || return 1
  line="$(sshd -T -C "user=${LOGIN_USER},host=localhost,addr=127.0.0.1" 2>/dev/null | awk 'tolower($1) == "authorizedkeysfile" {print; exit}')"
  read -r _ first second _ <<<"${line}"
  [[ "${first}" == ".ssh/authorized_keys" ]] || return 1
  [[ "${second}" == "${KEYS_DIR}/%u.engineers" || "${second}" == "${KEYS_FILE}" ]] || return 1
}

# Prints the "<type> <blob>" of every key pair instance metadata reports.
imds_key_pairs() {
  local token
  token="$(curl -fsS --max-time 5 -X PUT -H 'X-aws-ec2-metadata-token-ttl-seconds: 60' "${IMDS_BASE}/latest/api/token")" || return 1
  curl -fsS --max-time 5 -H "X-aws-ec2-metadata-token: ${token}" \
    "${IMDS_BASE}/latest/meta-data/public-keys/0/openssh-key" \
    | awk 'NF >= 2 {print $1, $2}'
}

# One-time prune of the legacy authorized_keys down to the EC2 key pair lines.
prune_legacy() {
  if [[ -f "${PRUNE_MARKER}" ]]; then
    PRUNE_STATE="already"
    return 0
  fi
  [[ -f "${KEYS_FILE}" ]] || { log "prune blocked, roster file missing"; return 1; }
  dropin_is_live || { log "prune blocked, sshd has not confirmed ${KEYS_FILE}"; return 1; }
  [[ -f "${LEGACY_FILE}" ]] || { log "prune blocked, ${LEGACY_FILE} missing"; return 1; }
  local key_pairs
  key_pairs="$(imds_key_pairs)" || { log "prune blocked, instance metadata unavailable"; return 1; }
  [[ -n "${key_pairs}" ]] || { log "prune blocked, instance metadata reports no key pair"; return 1; }

  local staging line type blob kept=0
  staging="$(mktemp -p "$(dirname "${LEGACY_FILE}")")" || { log "mktemp next to ${LEGACY_FILE} failed"; return 1; }
  while IFS= read -r line || [[ -n "${line}" ]]; do
    [[ -z "${line}" ]] && continue
    read -r type blob _ <<<"${line}"
    if grep -qxF -- "${type} ${blob}" <<<"${key_pairs}"; then
      printf '%s\n' "${line}" >>"${staging}" || { rm -f "${staging}"; log "write to the prune staging failed"; return 1; }
      kept=$((kept + 1))
    fi
  done <"${LEGACY_FILE}"
  if [[ "${kept}" -eq 0 || "$(grep -c . "${staging}" || :)" -ne "${kept}" ]]; then
    rm -f "${staging}"
    log "prune blocked, no key pair line found in ${LEGACY_FILE}"
    return 1
  fi

  local backup original_sha
  backup="${LEGACY_FILE}.pre-engineers.$(date -u +%Y%m%dT%H%M%SZ)"
  original_sha="$(sha256_of "${LEGACY_FILE}")"
  cp -p "${LEGACY_FILE}" "${backup}" || { rm -f "${staging}"; log "backup to ${backup} failed"; return 1; }
  if [[ "$(sha256_of "${backup}")" != "${original_sha}" ]]; then
    rm -f "${staging}" "${backup}"
    log "backup does not match ${LEGACY_FILE}"
    return 1
  fi
  chmod 0600 "${staging}" "${backup}" || { rm -f "${staging}"; log "chmod on the pruned file failed"; return 1; }
  own "${LOGIN_USER}" "${staging}" "${backup}" || { rm -f "${staging}"; log "chown on the pruned file failed"; return 1; }
  mv -f "${staging}" "${LEGACY_FILE}" || { log "rename into ${LEGACY_FILE} failed"; return 1; }
  date -u +%Y-%m-%dT%H:%M:%SZ >"${PRUNE_MARKER}" || { log "write of the prune marker failed"; return 1; }
  log "legacy authorized_keys pruned to ${kept} key pair line(s), backup ${backup}"
  PRUNE_STATE="done"
}

main() {
  mkdir -p "$(dirname "${LOCK_FILE}")" || die "cannot create the lock directory"
  exec 9>"${LOCK_FILE}"
  flock -w 30 9 || die "another sync holds ${LOCK_FILE}"

  install -d -m 0755 "${KEYS_DIR}" "${DROPIN_DIR}" || die "cannot create ${KEYS_DIR}"
  own root "${KEYS_DIR}" || die "cannot chown ${KEYS_DIR}"

  local roster version slugs=()
  roster="$(read_roster)"
  version="${roster%%$'\n'*}"
  version="${version#VERSION=}"
  ROSTER_VERSION="${version}"
  mapfile -t slugs < <(tail -n +2 <<<"${roster}")

  local staging slug added key_count=0
  staging="$(mktemp -p "${KEYS_DIR}")" || die "mktemp in ${KEYS_DIR} failed"
  for slug in "${slugs[@]}"; do
    if ! added="$(append_slug_keys "${slug}" "${staging}")"; then
      rm -f "${staging}"
      die "roster version ${version} not applied, last good file kept"
    fi
    key_count=$((key_count + added))
  done

  publish_keys_file "${staging}" "${key_count}" || { rm -f "${staging}"; die "roster version ${version} not published, last good file kept"; }
  KEY_COUNT="${key_count}"
  ensure_dropin || die "sshd drop-in not live"
  prune_legacy || die "legacy prune pending"

  write_metrics 1
  log "result=ok parameter_version=${version} engineers=${#slugs[@]} keys=${key_count} sha256=$(sha256_of "${KEYS_FILE}") file=${KEYS_STATE} sshd=${DROPIN_STATE} prune=${PRUNE_STATE}"
}

main "$@"
