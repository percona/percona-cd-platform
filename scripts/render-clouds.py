#!/usr/bin/env python3
# Copyright (C) 2026 Percona LLC
"""render-clouds: regenerate a Jenkins master's agent-cloud definitions from the
shared catalog + a per-master overlay, and keep the committed artifact in sync.

The catalog (resources/jenkins/clouds-catalog/) is the source of truth; the
ps3-clouds JCasC configScript embedded in the controller values is a GENERATED,
committed artifact (ADR 0029). `check` is the credential-free CI drift gate.

  render <host>   print the regenerated configScript (jenkins.clouds) to stdout
  apply  <host>   splice the regenerated configScript into the instance values.yaml
  check  <host>   regenerate and assert SEMANTIC equivalence to the committed
                  configScript; exit 1 on any drift (the `just ci` gate)

Run from anywhere; paths are resolved relative to this script's repo.
"""
import sys, os, re
import yaml

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
CATALOG_DIR = f"{REPO}/resources/jenkins/clouds-catalog"
# Per-host: the JCasC instance values file + the configScript key.
HOSTS = {
    "ps3": {
        "values": f"{REPO}/resources/jenkins/master/instances/ps3-k8s/values.yaml",
        "configScript": "ps3-clouds",
    },
}
# Helm/Sprig expr kept verbatim so the account id stays out of git (rendered by the chart `tpl`).
SPRIG = '{{ (splitList "." .Values.controller.image.registry) | first }}'
PLACEHOLDER = "ACCTID_EXPR"
def detpl(s): return re.sub(r"\{\{.*?\}\}", PLACEHOLDER, s)
def retpl(s): return s.replace(PLACEHOLDER, SPRIG)

class Dumper(yaml.SafeDumper): pass
Dumper.add_representer(str, lambda d, v: d.represent_scalar(
    "tag:yaml.org,2002:str", v, style="|" if "\n" in v else None))
# JCasC (SnakeYAML) caps YAML aliases at 50 and the reload throws ConfiguratorException
# above that. Spell every repeated node out fully (no anchors/aliases), exactly like the
# hand-written original, so the generated configScript loads on reload and on boot.
Dumper.ignore_aliases = lambda self, data: True
def dump(obj): return yaml.dump(obj, Dumper=Dumper, sort_keys=True,
                                default_flow_style=False, width=10**9, allow_unicode=True)

def catalog_overlay(host):
    cat = yaml.safe_load(open(f"{CATALOG_DIR}/catalog.yaml"))
    ov = yaml.safe_load(open(f"{CATALOG_DIR}/masters/{host}.yaml"))
    return cat, ov

def render_clouds(catalog, overlay):
    d = catalog["ec2_defaults"]; devices = catalog["devices"]; inits = catalog["initScripts"]
    o = overlay["ec2"]
    ec2_clouds = []
    for cl in o["clouds"]:
        tmpls = []
        for r in o["templates"]:
            t = dict(d)
            t.update({
                "ami": r["ami"], "type": r["type"], "description": r["os"],
                "labelString": r["labelString"], "customDeviceMapping": devices[r["dev"]],
                "ebsOptimized": r["ebsOptimized"], "initScript": inits[r["init"]],
                "instanceCapStr": r["instanceCapStr"], "numExecutors": r["numExecutors"],
                "remoteAdmin": r["remoteAdmin"], "jvmopts": r["jvmopts"],
                "subnetId": cl["subnetId"],
                "spotConfig": {"spotMaxBidPrice": r["spot"]["b" if cl["name"].endswith(" b") else "c"],
                               "useBidPrice": d["spotConfig"]["useBidPrice"]},
                "tags": [{"name": "Name", "value": f'{o["tagPrefix"]}{r["os"]}'},
                         {"name": "iit-billing-tag", "value": o["billingTag"]}],
            })
            # Per-row javaPath override (agent JVM from a non-PATH install,
            # e.g. the Temurin 21 tarball); default comes from ec2_defaults.
            if "javaPath" in r:
                t["javaPath"] = r["javaPath"]
            tmpls.append(t)
        ec2_clouds.append({"amazonEC2": {
            "name": cl["name"], "region": o["region"],
            "useInstanceProfileForCredentials": o["useInstanceProfileForCredentials"],
            "sshKeysCredentialsId": o["sshKeysCredentialsId"], "instanceCapStr": o["instanceCapStr"],
            "templates": tmpls}})
    hd = catalog["hetzner_defaults"]; ho = overlay["hetzner"]
    sts = [dict(hd, **row) for row in ho["serverTemplates"]]
    htz = {"hetzner": {"name": ho["name"], "credentialsId": ho["credentialsId"],
                       "instanceCapStr": ho["instanceCapStr"], "serverTemplates": sts}}
    fleet = {"eC2Fleet": overlay["eC2Fleet"]}
    return [htz] + ec2_clouds + [fleet]

def render_configscript(host):
    cat, ov = catalog_overlay(host)
    return retpl(dump({"jenkins": {"clouds": render_clouds(cat, ov)}}))

def committed_configscript(host):
    top = yaml.safe_load(open(HOSTS[host]["values"]))
    for root in (top.get("jenkins", {}), top):
        cs = (((root or {}).get("controller", {}) or {}).get("JCasC", {}) or {}).get("configScripts", {}) or {}
        if HOSTS[host]["configScript"] in cs:
            return cs[HOSTS[host]["configScript"]]
    raise SystemExit(f"{HOSTS[host]['configScript']} not found in {HOSTS[host]['values']}")

def diff(a, b, path=""):
    out = []
    if type(a) != type(b): return [f"{path}: {type(a).__name__}!={type(b).__name__}"]
    if isinstance(a, dict):
        for k in sorted(set(a) | set(b)):
            if k not in a: out.append(f"{path}.{k}: only-B")
            elif k not in b: out.append(f"{path}.{k}: only-A")
            else: out += diff(a[k], b[k], f"{path}.{k}")
    elif isinstance(a, list):
        if len(a) != len(b): out.append(f"{path}: len {len(a)}!={len(b)}")
        for i,(x,y) in enumerate(zip(a,b)): out += diff(x,y,f"{path}[{i}]")
    elif a != b: out.append(f"{path}: {a!r} != {b!r}")
    return out

def cmd_render(host): sys.stdout.write(render_configscript(host))

def cmd_apply(host):
    cfg = render_configscript(host).rstrip("\n")
    V = HOSTS[host]["values"]; key = HOSTS[host]["configScript"]
    lines = open(V).read().split("\n")
    ki = next(i for i, l in enumerate(lines) if re.match(rf"^(\s*){re.escape(key)}:\s*\|", l))
    key_indent = len(lines[ki]) - len(lines[ki].lstrip(" "))
    content_indent = key_indent + 2
    j = ki + 1
    while j < len(lines):
        l = lines[j]
        if l.strip() == "": j += 1; continue
        if len(l) - len(l.lstrip(" ")) <= key_indent: break
        j += 1
    new = [(" " * content_indent + cl).rstrip() if cl.strip() else "" for cl in cfg.split("\n")]
    out = lines[:ki + 1] + new + lines[j:]
    open(V, "w").write("\n".join(out))
    print(f"applied generated {key} into {os.path.relpath(V, REPO)} "
          f"(replaced lines {ki+2}..{j}, {len(new)} new content lines)")
    cmd_check(host)

def cmd_check(host):
    rendered = render_configscript(host)
    aliases = re.findall(r"(?:^|\s)[&*]id\d+\b", rendered)
    if aliases:
        print(f"DRIFT [{host}]: configScript emits {len(aliases)} YAML anchors/aliases; "
              f"JCasC (SnakeYAML) caps aliases at 50 and the reload fails. The dumper must ignore_aliases."); sys.exit(1)
    gen = yaml.safe_load(detpl(rendered))["jenkins"]["clouds"]
    com = yaml.safe_load(detpl(committed_configscript(host)))["jenkins"]["clouds"]
    shape = lambda cl: [(list(c)[0], (list(c.values())[0].get("name") or list(c.values())[0].get("fleet")),
                         len(list(c.values())[0].get("templates") or list(c.values())[0].get("serverTemplates") or []))
                        for c in cl]
    if shape(gen) != shape(com):
        print(f"DRIFT [{host}] cloud set differs:\n  generated {shape(gen)}\n  committed {shape(com)}"); sys.exit(1)
    d = diff(com, gen)
    if d:
        print(f"DRIFT [{host}] {len(d)} field diffs between catalog and committed configScript:")
        print("\n".join(d[:40])); sys.exit(1)
    print(f"OK [{host}]: committed {HOSTS[host]['configScript']} is in sync with the catalog "
          f"({shape(gen)}).")

if __name__ == "__main__":
    cmd = sys.argv[1] if len(sys.argv) > 1 else "check"
    host = sys.argv[2] if len(sys.argv) > 2 else "ps3"
    {"render": cmd_render, "apply": cmd_apply, "check": cmd_check}[cmd](host)
