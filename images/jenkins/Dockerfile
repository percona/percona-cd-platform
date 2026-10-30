# Base: Jenkins LTS 2.568.3 on Java 21. Pinned BY DIGEST so an
# upstream re-push of the same tag cannot change our base. Multi-arch INDEX
# digest (carries linux/amd64 + linux/arm64) so the one pin builds both platforms.
# Obtain/refresh with:
#   docker buildx imagetools inspect jenkins/jenkins:2.568.3-lts-jdk21 --format '{{.Manifest.Digest}}'
# 2.568.3 carries the 2026-06-10, 2026-08-05 and 2026-09-02 core security batches
# (CVE-2026-53435..53442, CVE-2026-70426..70430, CVE-2026-84645..84657), never backported
# to 2.541.x. Since 2.555.1 the controller AND the agent remoting JVMs require
# Java 21 or 25; this image line has run Java 21 since the 2.541.3-jdk21 pin.
FROM jenkins/jenkins:2.580.1-lts-jdk21@sha256:a660310e39ade10631f774bacd5219de767dfd08947ea5c32a739b5e5bb382c1

# Skip the setup wizard (configuration comes from the persistent volume)
ENV JAVA_OPTS="-Djenkins.install.runSetupWizard=false"

# Run pipes under bash with -o pipefail so a failure in a piped RUN (the
# effective-list generation below) is not masked by the pipe (hadolint DL4006).
SHELL ["/bin/bash", "-o", "pipefail", "-c"]

# Community plugins from the manifest. --latest=false makes resolution
# deterministic (use exactly the pinned versions; do not silently upgrade
# transitive deps to "latest"). --verbose for build-log auditability.
COPY plugins.txt /usr/share/jenkins/ref/plugins.txt

# Legal: license + attribution files, carried into the distributed image.
COPY LICENSE LICENSE.apache-2.0.txt NOTICE README.md /licenses/

RUN jenkins-plugin-cli \
      --plugin-file /usr/share/jenkins/ref/plugins.txt \
      --latest=false \
      --verbose

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
#   * COMMUNITY plugins stay SOFT. They are left as plain ref/plugins/<id>.jpi and
#     follow the jenkins-support rules: a missing plugin is seeded, a plugin whose
#     .version_from_image marker matches the home copy is upgraded when the image
#     carries a newer version, and a plugin upgraded by hand past its marker is
#     kept. A home without markers needs TRY_UPGRADE_IF_NO_MARKER (set per
#     instance) to converge. Plugins that exist only on the PVC are never removed.
#   * PINNED community plugins (PINNED_PLUGINS below) are FORCED like the forks:
#     plugins we manage exclusively through plugins.txt, where the image must win
#     over whatever the PVC carries. apache-httpcomponents-client-5-api is pinned
#     because the entrypoint turns '-' into '.' before comparing, so it ranks the
#     installed 5.6-183 above 5.6.4-204 and would never upgrade it.
RUN set -euo pipefail; \
    for fork in /usr/share/jenkins/ref/percona-plugins/*.jpi; do \
      id="$(basename "$fork" .jpi)"; \
      cp "$fork" "/usr/share/jenkins/ref/plugins/${id}.jpi.override"; \
      rm -f "/usr/share/jenkins/ref/plugins/${id}.jpi"; \
    done; \
    PINNED_PLUGINS="ec2-fleet apache-httpcomponents-client-5-api"; \
    for id in $PINNED_PLUGINS; do \
      test -f "/usr/share/jenkins/ref/plugins/${id}.jpi"; \
      mv "/usr/share/jenkins/ref/plugins/${id}.jpi" "/usr/share/jenkins/ref/plugins/${id}.jpi.override"; \
    done; \
    rm -rf /usr/share/jenkins/ref/percona-plugins
