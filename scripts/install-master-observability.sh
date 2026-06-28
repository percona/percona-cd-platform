#!/bin/bash
# install-master-observability.sh
#
# Master-side push pipeline. Installs amazon-ssm-agent and Grafana Alloy.
# Alloy scrapes /hetzner-prometheus and host OS metrics (built-in unix
# exporter), tails the Jenkins log, and pushes to the alloy-gateway ALB.
# The bearer is fetched from AWS Secrets Manager via ExecStartPre on each start.
#
# Caller must export JENKINS_HOST (e.g. ps80.cd.percona.com). Fetched at boot by
# the Terraform on-demand masters and re-run on a live master via SSM.
#
# Idempotent. A re-run validates the config and restarts Alloy so changes take
# effect (a plain enable --now would not reload a running unit).

set -euo pipefail

if [[ -z "${JENKINS_HOST:-}" ]]; then
    echo "FATAL: JENKINS_HOST not set" >&2
    exit 1
fi

MASTER_LABEL="${JENKINS_HOST%.percona.com}"

# 1. SSM agent for fleet-wide Run Command fan-out.
dnf -y install amazon-ssm-agent || yum -y install amazon-ssm-agent
systemctl enable --now amazon-ssm-agent

# 2. Grafana RPM repo + Alloy.
cat > /etc/yum.repos.d/grafana.repo <<'REPO'
[grafana]
name=grafana
baseurl=https://rpm.grafana.com
repo_gpgcheck=1
enabled=1
gpgcheck=1
gpgkey=https://rpm.grafana.com/gpg.key
sslverify=1
sslcacert=/etc/pki/tls/certs/ca-bundle.crt
REPO
dnf -y install alloy

# 3. Bearer fetcher. The secret is JSON, so parse the token with python3 and
# write it atomically. A failed fetch never leaves an empty token file (which
# would 401 every push).
cat > /usr/local/bin/alloy-fetch-token <<'SCRIPT'
#!/bin/bash
set -euo pipefail
umask 077
secret_json=$(aws secretsmanager get-secret-value \
    --region us-east-1 \
    --secret-id percona-ci-platform/alloy-gateway/bearer \
    --query SecretString --output text)
token=$(printf '%s' "${secret_json}" | python3 -c '
import json, sys
parsed = json.loads(sys.stdin.read())
for key in ("bearer_token", "bearer", "token"):
    if isinstance(parsed, dict) and key in parsed:
        print(parsed[key], end=""); break
else:
    print(parsed, end="")
')
[[ -n "${token}" ]] || { echo "alloy-fetch-token: empty bearer" >&2; exit 1; }
token_tmp=/etc/alloy/gateway-token.tmp
printf '%s' "${token}" > "${token_tmp}"
chown root:alloy "${token_tmp}"
chmod 0440 "${token_tmp}"
mv -f "${token_tmp}" /etc/alloy/gateway-token
SCRIPT
chmod 0755 /usr/local/bin/alloy-fetch-token

install -d -m 0755 /etc/systemd/system/alloy.service.d
# The `+` prefix runs the fetcher as root (the unit is User=alloy) so it can
# write the token file (0440 root:alloy).
cat > /etc/systemd/system/alloy.service.d/fetch-token.conf <<'DROPIN'
[Service]
ExecStartPre=+/usr/local/bin/alloy-fetch-token
Restart=on-failure
RestartSec=30s
DROPIN

# 4. Alloy config. The receiver path is /api/v1/metrics/write, not /api/v1/push.
install -d -o root -g alloy -m 0750 /etc/alloy

# Back up the current config outside /etc/alloy/ (so Alloy never parses it) for
# the rollback in section 5.
if [[ -f /etc/alloy/config.alloy ]]; then
    cp -a /etc/alloy/config.alloy /etc/alloy-config.alloy.bak
fi

cat > /etc/alloy/config.alloy <<CONFIG
prometheus.scrape "hetzner_local" {
  targets         = [{__address__ = "localhost:8080", __metrics_path__ = "/hetzner-prometheus/"}]
  forward_to      = [prometheus.remote_write.mimir.receiver]
  scrape_interval = "60s"
  scrape_timeout  = "15s"
}

prometheus.exporter.unix "node" {
  set_collectors = ["cpu", "meminfo", "filesystem", "diskstats", "netdev", "loadavg", "uname", "vmstat"]

  netdev {
    device_exclude = "^(veth.*|docker.*|br-.*|cni.*|lo)\$"
  }

  filesystem {
    fs_types_exclude     = "^(autofs|binfmt_misc|bpf|cgroup2?|configfs|debugfs|devpts|devtmpfs|fusectl|hugetlbfs|iso9660|mqueue|nsfs|overlay|proc|procfs|pstore|rpc_pipefs|securityfs|selinuxfs|squashfs|sysfs|tmpfs|tracefs)\$"
    mount_points_exclude = "^/(dev|proc|sys|run|var/lib/docker/.+)(\$|/)"
  }
}

prometheus.scrape "node" {
  targets         = prometheus.exporter.unix.node.targets
  forward_to      = [prometheus.remote_write.mimir.receiver]
  scrape_interval = "60s"
  scrape_timeout  = "15s"
}

prometheus.remote_write "mimir" {
  endpoint {
    url               = "https://mimir-push.cd.percona.com/api/v1/metrics/write"
    bearer_token_file = "/etc/alloy/gateway-token"
    headers           = { "X-Scope-OrgID" = "percona-ci" }
  }
  external_labels = {
    master = "${MASTER_LABEL}",
    fleet  = "percona-jenkins",
    role   = "master",
  }
}

loki.source.file "jenkins" {
  targets       = [{__path__ = "/var/log/jenkins/jenkins.log", master = "${MASTER_LABEL}", fleet = "percona-jenkins", role = "master", component = "jenkins"}]
  forward_to    = [loki.write.gateway.receiver]
  tail_from_end = false
}

loki.write "gateway" {
  endpoint {
    url               = "https://loki-push.cd.percona.com/loki/api/v1/push"
    bearer_token_file = "/etc/alloy/gateway-token"
  }
}
CONFIG
chown root:alloy /etc/alloy/config.alloy
chmod 0640 /etc/alloy/config.alloy

# 5. Validate, then restart so a re-run loads the rewritten config (enable --now
# only starts a stopped unit). Pre-fetch the bearer best-effort first.
systemctl daemon-reload
/usr/local/bin/alloy-fetch-token || true

if alloy validate --help >/dev/null 2>&1; then
    if ! alloy validate /etc/alloy/config.alloy; then
        echo "FATAL: /etc/alloy/config.alloy failed validation, restoring previous config" >&2
        if [[ -f /etc/alloy-config.alloy.bak ]]; then
            cp -a /etc/alloy-config.alloy.bak /etc/alloy/config.alloy
        fi
        exit 1
    fi
else
    echo "WARN: this alloy build has no 'validate' subcommand, skipping config validation" >&2
fi

systemctl enable alloy

# The unit is Type=simple, so restart returns 0 on fork even if the config then
# crashes Alloy. The is-active check below is authoritative and drives rollback.
if ! systemctl restart alloy; then
    echo "NOTE: 'systemctl restart alloy' returned non-zero, verifying with is-active" >&2
fi
sleep 2

if systemctl is-active --quiet alloy; then
    echo "alloy is active with the updated config"
else
    echo "FATAL: alloy failed to start with the new config, restoring previous config" >&2
    if [[ -f /etc/alloy-config.alloy.bak ]]; then
        cp -a /etc/alloy-config.alloy.bak /etc/alloy/config.alloy
        systemctl restart alloy || true
    fi
    exit 1
fi
