/**
 * pxc-upgraded clone fence stub. Same filename as production pxc's
 * jnlpHost.groovy, so the boot sync overwrites the copy inherited inside the
 * restored JENKINS_HOME. The production script advertises the chaos-testing
 * JNLP endpoint, which belongs to production pxc alone.
 */
println("[jnlpHost.groovy fence] no-op on the clone")
