/**
 * pxc-upgraded clone fence stub. Same filename as production pxc's
 * plugins.groovy, so the boot sync overwrites the copy inherited inside the
 * restored JENKINS_HOME. Plugin state on the clone is managed by the
 * validation runbook, never by a boot-time install hook.
 */
println("[plugins.groovy fence] no-op on the clone")
