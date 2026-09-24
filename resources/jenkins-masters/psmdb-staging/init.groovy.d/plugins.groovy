/**
 * psmdb-staging stub. Same filename as production psmdb's plugins.groovy, so the boot-time
 * S3 fetch and the 30-minute init sync overwrite the copy restored inside
 * JENKINS_HOME. The production script installs plugins and restarts Jenkins, which must never run on the staging copy.
 */
println("[plugins.groovy fence] no-op on the staging copy")
