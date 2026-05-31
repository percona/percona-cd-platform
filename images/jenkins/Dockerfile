# Base: Jenkins LTS 2.541.3 (matches the live fleet). Pinned BY DIGEST so an
# upstream re-push of the same tag cannot change our base. amd64 platform
# digest (EKS nodes are amd64; m3 build host is arm64 -> buildx cross-build).
# Obtain/refresh with:
#   crane digest --platform linux/amd64 jenkins/jenkins:2.541.3-lts-jdk17
# Resolved 2026-05-31: sha256:d28f21fc6daeb6c928556324a84b0548f1b42be2318231622c85a118f81f2e6d
FROM jenkins/jenkins:2.541.3-lts-jdk17@sha256:d28f21fc6daeb6c928556324a84b0548f1b42be2318231622c85a118f81f2e6d

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

# Custom Percona plugin forks (override update-center versions). Layered AFTER
# the CLI step so these win. Staged by fetch-hpis.sh as <id>.jpi (SHA-verified
# against percona-plugins.lock.json) BEFORE the build; the dir is committed
# (with a .gitkeep) so the COPY glob is never zero-file. Forks MUST be .jpi (not
# .hpi): the ref-dir seeder only version-matches *.jpi and core prefers a .jpi
# over a same-named .hpi, so a .hpi fork would lose to a restored PVC copy.
COPY --chown=jenkins:jenkins percona-plugins/*.jpi /usr/share/jenkins/ref/plugins/

# init.groovy.d (persistent + one-time groovy). Present in the tree today but
# never COPYed -> add it so the baked image carries the boot hooks.
COPY groovy/ /usr/share/jenkins/ref/init.groovy.d/

# Force the BAKED plugin set to win over a PVC-restored JENKINS_HOME. The jenkins
# entrypoint force-installs any ref/plugins/<id>.jpi.override by COPYing THAT
# file's content to $JENKINS_HOME/plugins/<id>.jpi -- so the .override file must
# BE the plugin (an empty marker would overwrite the plugin with 0 bytes and
# every plugin would fail to load). Rename every baked plugin to .override so the
# image is the source of truth on every boot. (Trade-off in README.md: a
# hand-pinned plugin on a live PVC is reset to the baked version on restart.)
RUN for plugin in /usr/share/jenkins/ref/plugins/*.jpi; do \
      mv "$plugin" "$plugin.override"; \
    done
