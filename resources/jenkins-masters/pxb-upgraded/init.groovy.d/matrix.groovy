/**
 * pxb-upgraded clone fence stub. Same filename as production pxb's
 * matrix.groovy, so the boot sync overwrites the copy inherited inside the
 * restored JENKINS_HOME. The inherited authorization matrix from config.xml stays as restored. Validation access is by pre-existing API tokens, browser OAuth is host-bound to pxb.cd.percona.com and stays broken on the clone by design.
 */
println("[matrix.groovy fence] no-op on the clone")
