FROM jenkins/jenkins:2.528.3-lts-jdk17

# Skip the setup wizard (configuration comes from the persistent volume)
ENV JAVA_OPTS="-Djenkins.install.runSetupWizard=false"

# Install plugins from the manifest
COPY plugins.txt /usr/share/jenkins/ref/plugins.txt
RUN jenkins-plugin-cli --plugin-file /usr/share/jenkins/ref/plugins.txt \
    --verbose

# Install custom Percona plugin forks (override update center versions)
COPY percona-plugins/*.hpi /usr/share/jenkins/ref/plugins/
