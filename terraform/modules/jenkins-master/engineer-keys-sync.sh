#!/usr/bin/env bash
#
# Reconcile the engineer SSH keys on a Jenkins master from the fleet roster in
# SSM Parameter Store (docs/adr/0046).
#
# Reads {"schema":1,"engineers":[...]} from ROSTER_PARAMETER, fetches every
# slug's public key from percona.com, validates each key line, and atomically
# replaces the dedicated key file that the sshd drop-in names. The contract:
#   - A slug that leaves the roster loses its keys on this run, whatever the
#     feed does for anybody else.
#   - A slug the feed answers with 404 is one IT has revoked (or a typo): its
#     keys are dropped, the run stays green and reports the slug as missing so
#     the roster gets cleaned up. If every slug 404s the feed itself is broken
#     and the run changes nothing.
#   - A slug whose fetch fails any other way (timeout, 5xx, TLS, malformed
#     body) keeps the keys it already had in the file (never invented, never
#     dropped on a transient), the run is reported as degraded and exits 1 so
#     the alert fires. The legacy prune waits for a run that applied the roster
#     in full.
#   - A roster that cannot be read or parsed changes nothing and exits 1.
#   - An empty roster writes an empty file without contacting percona.com.
# Failures before the roster file is published keep the last good file
# byte-identical. Failures after it (sshd reload, prune, metrics) leave the
# published roster in place and are reported by stage. Once sshd has validated
# AND reloaded the drop-in (recorded in a marker, so an interrupted run cannot
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
#   LOCK_WAIT          seconds to wait for the lock before leaving it to the holder
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
readonly LOCK_WAIT="${LOCK_WAIT:-300}"
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
DEGRADED_SLUGS=""
MISSING_SLUGS=""
LEGACY_PRUNED=""

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
# code. The payload is built in memory and written with one checked write, so
# a full disk can never replace the previous file with an empty one. The last
# success timestamp and the last fully applied roster version survive failed
# and degraded runs.
write_metrics() {
  local success="$1"
  local now previous_success previous_version staging payload keyset
  now="$(date -u +%s)"
  previous_success="$(awk '$1 == "engineer_keys_sync_last_success_timestamp_seconds" {print $2}' "${METRICS_FILE}" 2>/dev/null || :)"
  previous_version="$(awk '$1 == "engineer_keys_sync_roster_version" {print $2}' "${METRICS_FILE}" 2>/dev/null || :)"
  if [[ "${success}" -eq 1 ]]; then
    previous_success="${now}"
    previous_version="${ROSTER_VERSION}"
  fi
  local degraded_count=0 missing_count=0
  if [[ -n "${DEGRADED_SLUGS}" ]]; then
    degraded_count="$(tr ',' '\n' <<<"${DEGRADED_SLUGS}" | grep -c .)"
  fi
  if [[ -n "${MISSING_SLUGS}" ]]; then
    missing_count="$(tr ',' '\n' <<<"${MISSING_SLUGS}" | grep -c .)"
  fi
  payload="# HELP engineer_keys_sync_last_run_timestamp_seconds Unix time of the last reconciler run.
# TYPE engineer_keys_sync_last_run_timestamp_seconds gauge
engineer_keys_sync_last_run_timestamp_seconds ${now}
# HELP engineer_keys_sync_last_run_success 1 when the last run applied the roster in full, 0 when it failed or was degraded.
# TYPE engineer_keys_sync_last_run_success gauge
engineer_keys_sync_last_run_success ${success}
# HELP engineer_keys_sync_degraded_slugs Slugs whose feed fetch failed on the last run and kept their previous keys.
# TYPE engineer_keys_sync_degraded_slugs gauge
engineer_keys_sync_degraded_slugs ${degraded_count}
# HELP engineer_keys_sync_slugs_missing Roster slugs the feed answered 404 for on the last run, their keys are not installed.
# TYPE engineer_keys_sync_slugs_missing gauge
engineer_keys_sync_slugs_missing ${missing_count}
"
  if [[ -n "${LEGACY_PRUNED}" ]]; then
    payload+="# HELP engineer_keys_sync_legacy_pruned 1 once the legacy authorized_keys holds only the EC2 key pair.
# TYPE engineer_keys_sync_legacy_pruned gauge
engineer_keys_sync_legacy_pruned ${LEGACY_PRUNED}
"
  fi
  if [[ -n "${previous_success}" ]]; then
    payload+="# HELP engineer_keys_sync_last_success_timestamp_seconds Unix time of the last run that applied the roster in full.
# TYPE engineer_keys_sync_last_success_timestamp_seconds gauge
engineer_keys_sync_last_success_timestamp_seconds ${previous_success}
"
  fi
  if [[ -n "${previous_version}" ]]; then
    payload+="# HELP engineer_keys_sync_roster_version SSM parameter version of the roster last applied in full.
# TYPE engineer_keys_sync_roster_version gauge
engineer_keys_sync_roster_version ${previous_version}
"
  fi
  if [[ -n "${KEY_COUNT}" ]]; then
    payload+="# HELP engineer_keys_sync_keys Public keys in the managed roster file.
# TYPE engineer_keys_sync_keys gauge
engineer_keys_sync_keys ${KEY_COUNT}
"
  fi
  if [[ -f "${KEYS_FILE}" ]]; then
    keyset="$(sha256_of "${KEYS_FILE}")"
    payload+="# HELP engineer_keys_sync_keyset_info sha256 of the managed roster file, a label change means the installed key set changed.
# TYPE engineer_keys_sync_keyset_info gauge
engineer_keys_sync_keyset_info{keyset_sha256=\"${keyset}\"} 1
"
  fi
  install -d -m 0755 "${METRICS_DIR}" 2>/dev/null || { log "metrics directory unavailable"; return 0; }
  staging="$(mktemp -p "${METRICS_DIR}" 2>/dev/null)" || { log "metrics staging failed"; return 0; }
  if ! printf '%s' "${payload}" >"${staging}" || [[ ! -s "${staging}" ]]; then
    rm -f "${staging}"
    log "metrics write failed, previous metrics kept"
    return 0
  fi
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
    if not isinstance(slug, str) or not pattern.fullmatch(slug):
        sys.exit(2)
print(f"VERSION={parameter['Version']}")
for slug in sorted(set(engineers)):
    print(slug)
PY
}

# Fetches one slug's .pub, validates every line, and appends normalised
# "<type> <blob> <slug>" lines to the staging file. Prints the number of keys
# appended. Returns 4 when the feed answers 404 (the slug is gone upstream),
# 1 on any other fetch or validation problem, including a failed write.
append_slug_keys() {
  local slug="$1"
  local staging="$2"
  local url fetched validated code line type blob key_count=0
  # shellcheck disable=SC2059  # the template is the format string by design
  url="$(printf "${KEY_URL_TEMPLATE}" "${slug}")"
  fetched="$(mktemp)" || { log "mktemp failed"; return 1; }
  # Lines land in a per-slug file first and reach the shared staging file only
  # once every line of the feed validated. A slug that fails on its second key
  # therefore leaves nothing behind and takes the carry path cleanly.
  validated="$(mktemp)" || { rm -f "${fetched}"; log "mktemp failed"; return 1; }
  code="$(curl -sS --max-time 20 --retry 2 --retry-delay 2 -o "${fetched}" -w '%{http_code}' "${url}" 2>/dev/null)" || code="000"
  if [[ "${code}" == "404" ]]; then
    rm -f "${fetched}" "${validated}"
    log "feed has no key for ${slug} (404)"
    return 4
  fi
  if [[ "${code}" != "200" ]]; then
    rm -f "${fetched}" "${validated}"
    log "fetch failed for ${slug} (http ${code})"
    return 1
  fi
  local reason=""
  while IFS= read -r line || [[ -n "${line}" ]]; do
    line="${line%$'\r'}"
    [[ -z "${line}" ]] && continue
    if [[ ! "${line}" =~ ${KEY_TYPE_PATTERN} ]]; then
      reason="malformed key line for ${slug}"
      break
    fi
    if ! ssh-keygen -lf <(printf '%s\n' "${line}") >/dev/null 2>&1; then
      reason="key rejected by ssh-keygen for ${slug}"
      break
    fi
    read -r type blob _ <<<"${line}"
    if ! printf '%s %s %s\n' "${type}" "${blob}" "${slug}" >>"${validated}"; then
      reason="write of the validated keys failed for ${slug}"
      break
    fi
    key_count=$((key_count + 1))
  done <"${fetched}"
  rm -f "${fetched}"
  if [[ -z "${reason}" && "${key_count}" -eq 0 ]]; then
    reason="no key served for ${slug}"
  fi
  if [[ -n "${reason}" ]]; then
    rm -f "${validated}"
    log "${reason}"
    return 1
  fi
  if ! cat "${validated}" >>"${staging}"; then
    rm -f "${validated}"
    log "write to staging failed for ${slug}"
    return 1
  fi
  rm -f "${validated}"
  echo "${key_count}"
}

# Copies the lines the published file already holds for one slug into the
# staging file and prints how many. Zero is a valid answer (a slug that never
# had keys stays absent). Returns 1 only when the copy itself fails.
carry_previous_keys() {
  local slug="$1"
  local staging="$2"
  local line type blob comment count=0
  [[ -f "${KEYS_FILE}" ]] || { echo 0; return 0; }
  while IFS= read -r line || [[ -n "${line}" ]]; do
    [[ -z "${line}" ]] && continue
    read -r type blob comment _ <<<"${line}"
    [[ "${comment}" == "${slug}" ]] || continue
    printf '%s %s %s\n' "${type}" "${blob}" "${slug}" >>"${staging}" || return 1
    count=$((count + 1))
  done <"${KEYS_FILE}"
  echo "${count}"
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
    # Same content, but StrictModes ignores a group-writable or non-root file
    # without a word, so the mode and owner are re-asserted every run.
    chmod 0644 "${KEYS_FILE}" || { log "chmod on ${KEYS_FILE} failed"; return 1; }
    own root "${KEYS_FILE}" || { log "chown on ${KEYS_FILE} failed"; return 1; }
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
    cp -p "${DROPIN_FILE}" "${previous}" || { rm -f "${previous}"; log "backup of the drop-in failed"; return 1; }
    had_previous=1
  fi
  staging="$(mktemp -p "${DROPIN_DIR}")" || { rm -f "${previous}"; log "mktemp in ${DROPIN_DIR} failed"; return 1; }
  printf '%s\n' "${DROPIN_CONTENT}" >"${staging}" || { rm -f "${previous}" "${staging}"; log "write of the drop-in failed"; return 1; }
  chmod 0600 "${staging}" || { rm -f "${previous}" "${staging}"; log "chmod on the drop-in failed"; return 1; }
  own root "${staging}" || { rm -f "${previous}" "${staging}"; log "chown on the drop-in failed"; return 1; }
  mv -f "${staging}" "${DROPIN_FILE}" || { rm -f "${previous}" "${staging}"; log "rename of the drop-in failed"; return 1; }
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
# "Nothing to remove" (a fresh instance that never had engineer keys, or no
# legacy file at all) counts as done, so a rebuilt master never sits red on a
# prune that has no work. Blocked only when removing would take away the last
# line or instance metadata cannot say which line is the key pair.
prune_legacy() {
  if [[ -f "${PRUNE_MARKER}" ]]; then
    PRUNE_STATE="already"
    LEGACY_PRUNED=1
    return 0
  fi
  if [[ ! -f "${LEGACY_FILE}" ]] || ! grep -q . "${LEGACY_FILE}"; then
    date -u +%Y-%m-%dT%H:%M:%SZ >"${PRUNE_MARKER}" || { log "write of the prune marker failed"; return 1; }
    PRUNE_STATE="nothing"
    LEGACY_PRUNED=1
    return 0
  fi
  [[ -f "${KEYS_FILE}" ]] || { log "prune blocked, roster file missing"; return 1; }
  dropin_is_live || { log "prune blocked, sshd has not confirmed ${KEYS_FILE}"; return 1; }
  local key_pairs
  key_pairs="$(imds_key_pairs)" || { log "prune blocked, instance metadata unavailable"; return 1; }
  [[ -n "${key_pairs}" ]] || { log "prune blocked, instance metadata reports no key pair while ${LEGACY_FILE} has lines"; return 1; }

  # Every line that goes must be a key the roster file now serves. Anything
  # else (a hand-added key, an engineer the seeding missed) blocks the prune
  # and is named, so the one step that removes access never removes it from
  # somebody the roster does not know about.
  local staging line type blob comment fingerprint stranger="" kept=0 removed=0
  staging="$(mktemp -p "$(dirname "${LEGACY_FILE}")")" || { log "mktemp next to ${LEGACY_FILE} failed"; return 1; }
  while IFS= read -r line || [[ -n "${line}" ]]; do
    [[ -z "${line}" ]] && continue
    read -r type blob comment <<<"${line}"
    if grep -qxF -- "${type} ${blob}" <<<"${key_pairs}"; then
      printf '%s\n' "${line}" >>"${staging}" || { rm -f "${staging}"; log "write to the prune staging failed"; return 1; }
      kept=$((kept + 1))
      continue
    fi
    fingerprint="$(ssh-keygen -lf <(printf '%s %s\n' "${type}" "${blob}") 2>/dev/null | awk '{print $2}')"
    fingerprint="${fingerprint:-unparseable}"
    if ! grep -qF -- " ${blob} " "${KEYS_FILE}"; then
      stranger="${fingerprint} ${comment:-(no comment)}"
      break
    fi
    log "prune removes ${fingerprint} ${comment:-(no comment)}, now served by ${KEYS_FILE}"
    removed=$((removed + 1))
  done <"${LEGACY_FILE}"
  if [[ -n "${stranger}" ]]; then
    rm -f "${staging}"
    log "prune blocked, ${LEGACY_FILE} holds a key the roster does not serve: ${stranger}. Add its owner to the roster or remove the line by hand"
    return 1
  fi
  if [[ "${removed}" -eq 0 ]]; then
    rm -f "${staging}"
    date -u +%Y-%m-%dT%H:%M:%SZ >"${PRUNE_MARKER}" || { log "write of the prune marker failed"; return 1; }
    PRUNE_STATE="nothing"
    LEGACY_PRUNED=1
    return 0
  fi
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
  mv -f "${staging}" "${LEGACY_FILE}" || { rm -f "${staging}"; log "rename into ${LEGACY_FILE} failed"; return 1; }
  date -u +%Y-%m-%dT%H:%M:%SZ >"${PRUNE_MARKER}" || { log "write of the prune marker failed"; return 1; }
  log "legacy authorized_keys pruned to ${kept} key pair line(s), ${removed} removed, backup ${backup}"
  PRUNE_STATE="done"
  LEGACY_PRUNED=1
}

main() {
  mkdir -p "$(dirname "${LOCK_FILE}")" || die "cannot create the lock directory"
  exec 9>"${LOCK_FILE}"
  # An operator trigger can land while the scheduled run is still fetching.
  # The run holding the lock reports for both, so this one leaves quietly and
  # touches neither the files nor the metrics.
  if ! flock -w "${LOCK_WAIT}" 9; then
    log "another sync holds ${LOCK_FILE}, leaving it to report"
    exit 0
  fi

  # sshd reads the keys directory as the login user, so it is world-readable.
  # sshd_config.d ships as 0700 from the openssh-server package and stays so.
  install -d -m 0755 "${KEYS_DIR}" || die "cannot create ${KEYS_DIR}"
  install -d -m 0700 "${DROPIN_DIR}" || die "cannot create ${DROPIN_DIR}"
  own root "${KEYS_DIR}" "${DROPIN_DIR}" || die "cannot chown ${KEYS_DIR}"

  local roster version slugs=()
  roster="$(read_roster)"
  version="${roster%%$'\n'*}"
  version="${version#VERSION=}"
  ROSTER_VERSION="${version}"
  mapfile -t slugs < <(tail -n +2 <<<"${roster}")

  local staging slug added carried rc key_count=0 missing_count=0
  staging="$(mktemp -p "${KEYS_DIR}")" || die "mktemp in ${KEYS_DIR} failed"
  for slug in "${slugs[@]}"; do
    rc=0
    added="$(append_slug_keys "${slug}" "${staging}")" || rc=$?
    if [[ "${rc}" -eq 0 ]]; then
      key_count=$((key_count + added))
      continue
    fi
    if [[ "${rc}" -eq 4 ]]; then
      # IT no longer publishes this slug. Honour it: no keys, reported so the
      # roster gets cleaned up, the run stays green.
      MISSING_SLUGS="${MISSING_SLUGS:+${MISSING_SLUGS},}${slug}"
      missing_count=$((missing_count + 1))
      continue
    fi
    # The feed failed for this slug only. Keep the keys it already has in the
    # published file (a roster removal never depends on this path), report the
    # run as degraded, and let the other slugs converge.
    carried="$(carry_previous_keys "${slug}" "${staging}")" || { rm -f "${staging}"; die "roster version ${version} not applied, could not carry the previous keys of ${slug}"; }
    key_count=$((key_count + carried))
    DEGRADED_SLUGS="${DEGRADED_SLUGS:+${DEGRADED_SLUGS},}${slug}"
    log "degraded: ${slug} keeps its ${carried} previous key(s)"
  done
  if [[ "${#slugs[@]}" -gt 0 && "${missing_count}" -eq "${#slugs[@]}" ]]; then
    rm -f "${staging}"
    die "roster version ${version} not applied, the feed answered 404 for every slug, last good file kept"
  fi

  publish_keys_file "${staging}" "${key_count}" || { rm -f "${staging}"; die "roster version ${version} not published, last good file kept"; }
  KEY_COUNT="${key_count}"
  ensure_dropin || die "roster published, sshd drop-in not live"
  # The one-time prune removes whatever static access the legacy file still
  # grants, so it only runs after a run that applied the roster in full.
  if [[ -n "${DEGRADED_SLUGS}" ]]; then
    PRUNE_STATE="deferred"
  else
    LEGACY_PRUNED=0
    prune_legacy || die "roster published, legacy prune pending"
  fi

  if [[ -n "${DEGRADED_SLUGS}" ]]; then
    write_metrics 0
    log "FAIL result=degraded parameter_version=${version} engineers=${#slugs[@]} keys=${key_count} degraded_slugs=${DEGRADED_SLUGS} missing_slugs=${MISSING_SLUGS:-none} sha256=$(sha256_of "${KEYS_FILE}") file=${KEYS_STATE} sshd=${DROPIN_STATE} prune=${PRUNE_STATE}"
    exit 1
  fi
  write_metrics 1
  log "result=ok parameter_version=${version} applied_version=${version} engineers=${#slugs[@]} keys=${key_count} missing_slugs=${MISSING_SLUGS:-none} sha256=$(sha256_of "${KEYS_FILE}") file=${KEYS_STATE} sshd=${DROPIN_STATE} prune=${PRUNE_STATE}"
}

main "$@"
