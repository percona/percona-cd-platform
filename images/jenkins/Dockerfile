# Base: Jenkins LTS 2.541.3 (matches the live fleet). Pinned BY DIGEST so an
# upstream re-push of the same tag cannot change our base. amd64 platform
# digest (EKS nodes are amd64; m3 build host is arm64 -> buildx cross-build).
# Obtain/refresh with:
#   crane digest --platform linux/amd64 jenkins/jenkins:2.541.3-lts-jdk17
# Resolved 2026-05-31: sha256:d28f21fc6daeb6c928556324a84b0548f1b42be2318231622c85a118f81f2e6d
FROM jenkins/jenkins:2.541.3-lts-jdk17@sha256:d28f21fc6daeb6c928556324a84b0548f1b42be2318231622c85a118f81f2e6d

# Skip the setup wizard (configuration comes from the persistent volume)
ENV JAVA_OPTS="-Djenkins.install.runSetupWizard=false"

# Community plugins from the manifest. --latest=false makes resolution
# deterministic (use exactly the pinned versions; do not silently upgrade
# transitive deps to "latest"). --verbose for build-log auditability.
COPY plugins.txt /usr/share/jenkins/ref/plugins.txt
RUN jenkins-plugin-cli \
      --plugin-file /usr/share/jenkins/ref/plugins.txt \
      --latest=false \
      --verbose \
 && cp /usr/share/jenkins/ref/plugins.txt /usr/share/jenkins/ref/plugins.txt.effective \
 && ls -1 /usr/share/jenkins/ref/plugins/ | sed 's/\.jpi$//' \
      > /usr/share/jenkins/ref/plugins.effective.txt

# Custom Percona plugin forks (override update-center versions). Layered AFTER
# the CLI step so these .hpi win. Staged by fetch-hpis.sh (SHA-verified against
# percona-plugins.lock.json) BEFORE the build; the dir is committed (with a
# .gitkeep) so the COPY glob is never zero-file.
COPY percona-plugins/*.hpi /usr/share/jenkins/ref/plugins/

# init.groovy.d (persistent + one-time groovy). Present in the tree today but
# never COPYed -> add it so the baked image carries the boot hooks.
COPY groovy/ /usr/share/jenkins/ref/init.groovy.d/

# Reconciliation markers: force the BAKED plugin set to win over a PVC-restored
# JENKINS_HOME. See README.md (.override decision). Generated from the effective
# list so it always matches what was actually installed.
RUN for plugin in /usr/share/jenkins/ref/plugins/*.jpi /usr/share/jenkins/ref/plugins/*.hpi; do \
      if [ -e "$plugin" ]; then touch "$plugin.override"; fi; \
    done
