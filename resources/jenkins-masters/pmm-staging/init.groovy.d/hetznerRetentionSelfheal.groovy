/**
 * pmm-staging stub. Same filename as production pmm's hetznerRetentionSelfheal.groovy, so the boot-time
 * S3 fetch and the 30-minute init sync overwrite the copy restored inside
 * JENKINS_HOME. The retention selfheal walks Hetzner nodes the staging copy must never touch.
 */
println("[hetznerRetentionSelfheal.groovy fence] no-op on the staging copy")
