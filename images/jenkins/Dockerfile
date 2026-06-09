# Base: Jenkins LTS 2.541.3 (matches the live fleet). Pinned BY DIGEST so an
# upstream re-push of the same tag cannot change our base. amd64 platform
# digest (EKS nodes are amd64; m3 build host is arm64 -> buildx cross-build).
# Obtain/refresh with:
#   crane digest --platform linux/amd64 jenkins/jenkins:2.541.3-lts-jdk17
# Resolved 2026-05-31: sha256:d28f21fc6daeb6c928556324a84b0548f1b42be2318231622c85a118f81f2e6d
FROM jenkins/jenkins:2.541.3-lts-jdk17@sha256:cce1eeb79902722f3cf7a8831c41e95c39f4a42b9a2981959630cc66539b3c2d

# Skip the setup wizard (configuration comes from the persistent volume)
ENV JAVA_OPTS="-Djenkins.install.runSetupWizard=false"

# Run pipes under bash with -o pipefail so a failure in a piped RUN (the
# effective-list generation below) is not masked by the pipe (hadolint DL4006).
SHELL ["/bin/bash", "-o", "pipefail", "-c"]

# Community plugins from the manifest. --latest=false makes resolution
# deterministic (use exactly the pinned versions; do not silently upgrade
# transitive deps to "latest"). --verbose for build-log auditability.
COPY plugins.txt /usr/share/jenkins/ref/plugins.txt
RUN jenkins-plugin-cli \
      --plugin-file /usr/share/jenkins/ref/plugins.txt \
      --latest=false \
      --verbose \
 && cp /usr/share/jenkins/ref/plugins.txt /usr/share/jenkins/ref/plugins.txt.effective \
 && find /usr/share/jenkins/ref/plugins/ -maxdepth 1 -name '*.jpi' -printf '%f\n' \
      | sed 's/\.jpi$//' | sort > /usr/share/jenkins/ref/plugins.effective.txt

# init.groovy.d (persistent + one-time groovy). Present in the tree today but
# never COPYed -> add it so the baked image carries the boot hooks.
COPY groovy/ /usr/share/jenkins/ref/init.groovy.d/

# Custom Percona plugin FORKS (hetzner-cloud, ec2). Staged by fetch-hpis.sh as
# <id>.jpi (SHA-verified against percona-plugins.lock.json) BEFORE the build; the
# dir is committed (with a .gitkeep) so the COPY glob is never zero-file. COPYed
# into a STAGING dir (not straight into ref/plugins) so the RUN below knows
# exactly which ids are forks. --chown=jenkins:jenkins so the ref-dir seeder,
# which runs as the jenkins user, can read the resulting .override files.
COPY --chown=jenkins:jenkins percona-plugins/*.jpi /usr/share/jenkins/ref/percona-plugins/

# HYBRID plugin-reconcile policy (see README.md "Plugin reconciliation"):
#   * FORKS are FORCED. The jenkins entrypoint force-installs any
#     ref/plugins/<id>.jpi.override by COPYing THAT file's content over
#     $JENKINS_HOME/plugins/<id>.jpi on every boot -- so the .override file must
#     BE the plugin (an empty marker would 0-byte it and the plugin would fail to
#     load). We rename ONLY the two forks to .override, so the patched ec2 /
#     hetzner-cloud always win over whatever a PVC-restored JENKINS_HOME carries.
#   * COMMUNITY plugins stay SOFT. They are left as plain ref/plugins/<id>.jpi, so
#     the seeder version-compares and seeds only when the home lacks the plugin or
#     carries an OLDER build. A community plugin upgraded via the UI on a live
#     master survives a pod restart (the reconcile point we deliberately keep).
RUN set -euo pipefail; \
    for fork in /usr/share/jenkins/ref/percona-plugins/*.jpi; do \
      id="$(basename "$fork" .jpi)"; \
      cp "$fork" "/usr/share/jenkins/ref/plugins/${id}.jpi.override"; \
      rm -f "/usr/share/jenkins/ref/plugins/${id}.jpi"; \
    done; \
    rm -rf /usr/share/jenkins/ref/percona-plugins
