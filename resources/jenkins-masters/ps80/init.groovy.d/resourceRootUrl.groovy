/**
 * Set ps80's resource root URL (Manage Jenkins > Security > Resource Root
 * URL) to https://ps80-assets.cd.percona.com/ (ADR 0047).
 *
 * On the main host Jenkins serves every archived build file with a sandbox
 * Content-Security-Policy that blocks scripts and inline styles, so an HTML
 * report with embedded JS and CSS renders broken. With a resource root URL
 * set, a request for such a file on ps80.cd.percona.com is redirected to a
 * tokenized URL on the resource host, where Jenkins serves the file without
 * that header. The token names the requesting user and expires after 30
 * minutes. An expired token redirects back to the main host, which applies
 * the GitHub login and job permissions again. The resource host answers
 * nothing else (404), so there is nothing to list or crawl.
 *
 * The host is routed by the jenkins-ingress chart (entry ps80-assets) and
 * must resolve before this setting lands, else every artifact link
 * redirects to a dead host. An empty RESOURCE_ROOT_URL clears the setting.
 *
 * ResourceDomainConfiguration.setUrl() validates, saves, and silently keeps
 * the old value when it rejects the URL, so the result is read back and a
 * mismatch fails the script. Idempotent: a matching value is a no-op.
 */
import jenkins.security.ResourceDomainConfiguration

def RESOURCE_ROOT_URL = 'https://ps80-assets.cd.percona.com/'

def config = ResourceDomainConfiguration.get()
def wanted = RESOURCE_ROOT_URL ?: null
def current = config.url

if (current == wanted) {
    println("resourceRootUrl: unchanged (${current ?: 'unset'})")
    return
}

config.url = wanted
if (config.url != wanted) {
    throw new IllegalStateException(
        "resourceRootUrl: Jenkins rejected ${wanted}, still ${config.url ?: 'unset'}")
}
println("resourceRootUrl: ${current ?: 'unset'} -> ${wanted ?: 'unset'}")
