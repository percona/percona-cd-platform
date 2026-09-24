/**
 * cloud-staging stub. Same filename as production cloud's matrix.groovy, so the boot-time
 * S3 fetch and the 30-minute init sync overwrite the copy restored inside
 * JENKINS_HOME. The authorization matrix restored from config.xml stays as it is. The staging copy switches to local login separately.
 */
println("[matrix.groovy fence] no-op on the staging copy")
