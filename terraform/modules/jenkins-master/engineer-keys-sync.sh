#!/usr/bin/env bash
#
# Reconcile the engineer SSH keys on a Jenkins master from the fleet roster in
# SSM Parameter Store (docs/adr/0046).
#
# Reads {"schema":1,"engineers":[...]} from ROSTER_PARAMETER, fetches every
# slug's public key from percona.com, validates each key line, and atomically
# replaces the dedicated key file that the sshd drop-in names. Any failure keeps
# the last good file byte-identical and exits non-zero. An empty roster writes
# an empty file without contacting percona.com. Once the dedicated file is live
# in sshd, the legacy engineer keys are pruned from the login user's
# authorized_keys one time, with a dated backup, leaving only the EC2 key pair
# that instance metadata reports.
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

readonly KEYS_DIR="${SSH_CONFIG_DIR}/authorized_keys.d"
readonly KEYS_FILE="${KEYS_DIR}/${LOGIN_USER}.engineers"
readonly DROPIN_FILE="${SSH_CONFIG_DIR}/sshd_config.d/40-engineer-keys.conf"
readonly DROPIN_CONTENT="AuthorizedKeysFile .ssh/authorized_keys ${KEYS_DIR}/%u.engineers"
readonly PRUNE_MARKER="${KEYS_DIR}/.legacy-pruned"
readonly LEGACY_FILE="${LOGIN_HOME}/.ssh/authorized_keys"
readonly SLUG_PATTERN='^[a-z0-9][a-z0-9._-]{0,63}$'
readonly KEY_TYPE_PATTERN='^(ssh-ed25519|ssh-rsa|ecdsa-sha2-nistp(256|384|521)|sk-ssh-ed25519@openssh\.com|sk-ecdsa-sha2-nistp256@openssh\.com) [A-Za-z0-9+/=]+( .*)?$'
readonly LOG_TAG="engineer-keys-sync"

# Log lines go to the journal and to stderr. Several helpers run inside
# command substitutions that capture stdout for their one-word state, so a
# message on stdout there would vanish.
log() {
  local message="$*"
  if command -v logger >/dev/null 2>&1; then
    logger -t "${LOG_TAG}" -- "${message}" || :
  fi
  echo "${LOG_TAG}: ${message}" >&2
}

die() {
  log "FAIL $*"
  exit 1
}

# install(1) owner flags only when running as root, so the tests can run as a
# normal user against a scratch root.
owner_flags() {
  local user="$1"
  if [[ "${EUID}" -eq 0 ]]; then
    printf -- '-o %s -g %s' "${user}" "${user}"
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
# "<type> <blob> <slug>" lines to the staging file. Returns 1 on any problem.
append_slug_keys() {
  local slug="$1"
  local staging="$2"
  local url fetched line type blob
  # shellcheck disable=SC2059  # the template is the format string by design
  url="$(printf "${KEY_URL_TEMPLATE}" "${slug}")"
  fetched="$(mktemp)"
  if ! curl -fsS --max-time 20 --retry 2 --retry-delay 2 -o "${fetched}" "${url}"; then
    rm -f "${fetched}"
    log "fetch failed for ${slug}"
    return 1
  fi
  local key_count=0
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
    printf '%s %s %s\n' "${type}" "${blob}" "${slug}" >>"${staging}"
    key_count=$((key_count + 1))
  done <"${fetched}"
  rm -f "${fetched}"
  if [[ "${key_count}" -eq 0 ]]; then
    log "no key served for ${slug}"
    return 1
  fi
}

file_sha256() {
  local path="$1"
  if [[ -f "${path}" ]]; then
    sha256sum "${path}" | awk '{print $1}'
  else
    echo "absent"
  fi
}

# Writes the assembled roster file atomically. Prints "changed" or "unchanged".
install_keys_file() {
  local staging="$1"
  local before after
  before="$(file_sha256 "${KEYS_FILE}")"
  after="$(sha256sum "${staging}" | awk '{print $1}')"
  if [[ "${before}" == "${after}" ]]; then
    rm -f "${staging}"
    echo unchanged
    return 0
  fi
  chmod 0644 "${staging}"
  if [[ "${EUID}" -eq 0 ]]; then
    chown root:root "${staging}"
  fi
  mv -f "${staging}" "${KEYS_FILE}"
  echo changed
}

# Installs the sshd drop-in when missing or different, validates the whole
# sshd configuration, and reloads sshd. Prints "reloaded" or "kept".
ensure_dropin() {
  if [[ -f "${DROPIN_FILE}" ]] && [[ "$(cat "${DROPIN_FILE}")" == "${DROPIN_CONTENT}" ]]; then
    echo kept
    return 0
  fi
  local staging
  staging="$(mktemp -p "$(dirname "${DROPIN_FILE}")")"
  printf '%s\n' "${DROPIN_CONTENT}" >"${staging}"
  chmod 0644 "${staging}"
  if [[ "${EUID}" -eq 0 ]]; then
    chown root:root "${staging}"
  fi
  mv -f "${staging}" "${DROPIN_FILE}"
  if ! sshd -t; then
    rm -f "${DROPIN_FILE}"
    die "sshd rejected the configuration with the drop-in, drop-in removed"
  fi
  systemctl reload sshd || die "sshd reload failed"
  echo reloaded
}

# True when the running sshd resolves AuthorizedKeysFile to both files.
dropin_is_live() {
  sshd -T -C "user=${LOGIN_USER},host=localhost,addr=127.0.0.1" 2>/dev/null \
    | grep -qiE "^authorizedkeysfile .*${KEYS_DIR}/"
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
# Prints "done", "already", or fails.
prune_legacy() {
  if [[ -f "${PRUNE_MARKER}" ]]; then
    echo already
    return 0
  fi
  [[ -f "${KEYS_FILE}" ]] || die "prune blocked, roster file missing"
  dropin_is_live || die "prune blocked, sshd does not resolve ${KEYS_FILE}"
  [[ -f "${LEGACY_FILE}" ]] || die "prune blocked, ${LEGACY_FILE} missing"
  local key_pairs
  key_pairs="$(imds_key_pairs)" || die "prune blocked, instance metadata unavailable"
  [[ -n "${key_pairs}" ]] || die "prune blocked, instance metadata reports no key pair"
  local staging line type blob kept=0
  staging="$(mktemp -p "$(dirname "${LEGACY_FILE}")")"
  while IFS= read -r line || [[ -n "${line}" ]]; do
    [[ -z "${line}" ]] && continue
    read -r type blob _ <<<"${line}"
    if grep -qxF -- "${type} ${blob}" <<<"${key_pairs}"; then
      printf '%s\n' "${line}" >>"${staging}"
      kept=$((kept + 1))
    fi
  done <"${LEGACY_FILE}"
  if [[ "${kept}" -eq 0 ]]; then
    rm -f "${staging}"
    die "prune blocked, no key pair line found in ${LEGACY_FILE}"
  fi
  local backup
  backup="${LEGACY_FILE}.pre-engineers.$(date -u +%Y%m%dT%H%M%SZ)"
  cp -p "${LEGACY_FILE}" "${backup}"
  chmod 0600 "${staging}" "${backup}"
  if [[ "${EUID}" -eq 0 ]]; then
    chown "${LOGIN_USER}:${LOGIN_USER}" "${staging}" "${backup}"
  fi
  mv -f "${staging}" "${LEGACY_FILE}"
  date -u +%Y-%m-%dT%H:%M:%SZ >"${PRUNE_MARKER}"
  log "legacy authorized_keys pruned to ${kept} key pair line(s), backup ${backup}"
  echo "done"
}

main() {
  mkdir -p "$(dirname "${LOCK_FILE}")"
  exec 9>"${LOCK_FILE}"
  flock -w 30 9 || die "another sync holds ${LOCK_FILE}"

  # shellcheck disable=SC2046  # owner_flags prints zero or four words on purpose
  install -d -m 0755 $(owner_flags root) "${KEYS_DIR}" "$(dirname "${DROPIN_FILE}")"

  local roster version slugs=()
  roster="$(read_roster)"
  version="${roster%%$'\n'*}"
  version="${version#VERSION=}"
  mapfile -t slugs < <(tail -n +2 <<<"${roster}")

  local staging slug failed=0
  staging="$(mktemp -p "${KEYS_DIR}")"
  for slug in "${slugs[@]}"; do
    if ! append_slug_keys "${slug}" "${staging}"; then
      failed=1
      break
    fi
  done
  if [[ "${failed}" -ne 0 ]]; then
    rm -f "${staging}"
    die "roster version ${version} not applied, last good file kept"
  fi
  local key_count
  key_count="$(grep -c . "${staging}" || :)"

  local keys_state dropin_state prune_state
  keys_state="$(install_keys_file "${staging}")"
  dropin_state="$(ensure_dropin)"
  prune_state="$(prune_legacy)"

  log "result=ok parameter_version=${version} engineers=${#slugs[@]} keys=${key_count} sha256=$(file_sha256 "${KEYS_FILE}") file=${keys_state} sshd=${dropin_state} prune=${prune_state}"
}

main "$@"
