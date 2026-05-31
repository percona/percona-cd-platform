// jnlpHost.groovy  (PS-11228) -- REMOVABLE WORKAROUND
//
// pxc is EKS-fronted: pxc.cd resolves to the shared ALB (HTTP/HTTPS only). But the
// CHAOS team's `test-chaos-vm` is a DIRECT-TCP inbound JNLP agent (webSocket=false)
// that connects from the corp DC (40.143.89.204/30) to the master's advertised
// inbound-agent host on :50000. By default the master advertises its rootUrl host
// (pxc.cd), which post-cutover points at the ALB (no :50000) and would break the
// agent. This pins the advertised inbound-agent host to the retained EIP so the
// agent connects to 13.56.198.107:50000 transparently (the SG allows the DC range).
//
// REMOVE this file (and create_eip=false + -replace) when test-chaos-vm is retired
// or converted to WebSocket. PS-11228.
import jenkins.model.Jenkins
import java.util.logging.Logger

final Logger LOG = Logger.getLogger('jnlpHost')
final String EIP = '13.56.198.107'
try {
    // CLI_HOST_NAME is the public static field TcpSlaveAgentListener.getAdvertisedHost() returns.
    hudson.TcpSlaveAgentListener.CLI_HOST_NAME = EIP
    System.setProperty('hudson.TcpSlaveAgentListener.hostName', EIP)
    if (Jenkins.instance.slaveAgentPort != 50000) Jenkins.instance.setSlaveAgentPort(50000)
    LOG.info("jnlpHost: inbound-agent advertised host pinned to ${EIP}:50000 (CHAOS test-chaos-vm)")
} catch (Throwable t) {
    LOG.warning("jnlpHost: failed to pin advertised host: ${t}")
}
