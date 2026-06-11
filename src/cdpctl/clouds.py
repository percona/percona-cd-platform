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

import argparse
import json
import os
import re
import sys

import yaml

from cdpctl import _stage
from cdpctl._repo import load_yaml, repo_root


def catalog_dir() -> str:
    return f"{repo_root()}/resources/jenkins/clouds-catalog"


# Per-host: the JCasC instance values file + the configScript key.
def hosts() -> dict:
    return {
        "ps3": {
            "values": f"{repo_root()}/resources/jenkins/master/instances/ps3-k8s/values.yaml",
            "configScript": "ps3-clouds",
        },
    }


# Helm/Sprig expr kept verbatim so the account id stays out of git (rendered by the chart `tpl`).
SPRIG = '{{ (splitList "." .Values.controller.image.registry) | first }}'
PLACEHOLDER = "ACCTID_EXPR"


def detpl(s):
    return re.sub(r"\{\{.*?\}\}", PLACEHOLDER, s)


def retpl(s):
    return s.replace(PLACEHOLDER, SPRIG)


class Dumper(yaml.SafeDumper):
    # JCasC (SnakeYAML) caps YAML aliases at 50 and the reload throws
    # ConfiguratorException above that. Spell every repeated node out fully
    # (no anchors/aliases), exactly like the hand-written original, so the
    # generated configScript loads on reload and on boot.
    def ignore_aliases(self, data):
        return True


Dumper.add_representer(
    str,
    lambda d, v: d.represent_scalar("tag:yaml.org,2002:str", v, style="|" if "\n" in v else None),
)


def dump(obj):
    return yaml.dump(
        obj,
        Dumper=Dumper,
        sort_keys=True,
        default_flow_style=False,
        width=10**9,
        allow_unicode=True,
    )


def catalog_overlay(host):
    cat = load_yaml(f"{catalog_dir()}/catalog.yaml")
    ov = load_yaml(f"{catalog_dir()}/masters/{host}.yaml")
    return cat, ov


def render_clouds(catalog, overlay):
    d = catalog["ec2_defaults"]
    devices = catalog["devices"]
    inits = catalog["initScripts"]
    o = overlay["ec2"]
    ec2_clouds = []
    for cl in o["clouds"]:
        tmpls = []
        for r in o["templates"]:
            t = dict(d)
            t.update(
                {
                    "ami": r["ami"],
                    "type": r["type"],
                    "description": r["os"],
                    "labelString": r["labelString"],
                    "customDeviceMapping": devices[r["dev"]],
                    "ebsOptimized": r["ebsOptimized"],
                    "initScript": inits[r["init"]],
                    "instanceCapStr": r["instanceCapStr"],
                    "numExecutors": r["numExecutors"],
                    "remoteAdmin": r["remoteAdmin"],
                    "jvmopts": r["jvmopts"],
                    "subnetId": cl["subnetId"],
                    "spotConfig": {
                        "spotMaxBidPrice": r["spot"]["b" if cl["name"].endswith(" b") else "c"],
                        "useBidPrice": d["spotConfig"]["useBidPrice"],
                    },
                    "tags": [
                        {"name": "Name", "value": f"{o['tagPrefix']}{r['os']}"},
                        {"name": "iit-billing-tag", "value": o["billingTag"]},
                    ],
                }
            )
            tmpls.append(t)
        ec2_clouds.append(
            {
                "amazonEC2": {
                    "name": cl["name"],
                    "region": o["region"],
                    "useInstanceProfileForCredentials": o["useInstanceProfileForCredentials"],
                    "sshKeysCredentialsId": o["sshKeysCredentialsId"],
                    "instanceCapStr": o["instanceCapStr"],
                    "templates": tmpls,
                }
            }
        )
    hd = catalog["hetzner_defaults"]
    ho = overlay["hetzner"]
    sts = [dict(hd, **row) for row in ho["serverTemplates"]]
    htz = {
        "hetzner": {
            "name": ho["name"],
            "credentialsId": ho["credentialsId"],
            "instanceCapStr": ho["instanceCapStr"],
            "serverTemplates": sts,
        }
    }
    fleet = {"eC2Fleet": overlay["eC2Fleet"]}
    return [htz] + ec2_clouds + [fleet]


def render_configscript(host):
    cat, ov = catalog_overlay(host)
    return retpl(dump({"jenkins": {"clouds": render_clouds(cat, ov)}}))


def committed_configscript(host):
    top = load_yaml(hosts()[host]["values"])
    for root in (top.get("jenkins", {}), top):
        cs = (((root or {}).get("controller", {}) or {}).get("JCasC", {}) or {}).get(
            "configScripts", {}
        ) or {}
        if hosts()[host]["configScript"] in cs:
            return cs[hosts()[host]["configScript"]]
    raise SystemExit(f"{hosts()[host]['configScript']} not found in {hosts()[host]['values']}")


def diff(a, b, path=""):
    out = []
    if type(a) is not type(b):
        return [f"{path}: {type(a).__name__}!={type(b).__name__}"]
    if isinstance(a, dict):
        for k in sorted(set(a) | set(b)):
            if k not in a:
                out.append(f"{path}.{k}: only-B")
            elif k not in b:
                out.append(f"{path}.{k}: only-A")
            else:
                out += diff(a[k], b[k], f"{path}.{k}")
    elif isinstance(a, list):
        if len(a) != len(b):
            out.append(f"{path}: len {len(a)}!={len(b)}")
        for i, (x, y) in enumerate(zip(a, b, strict=False)):
            out += diff(x, y, f"{path}[{i}]")
    elif a != b:
        out.append(f"{path}: {a!r} != {b!r}")
    return out


def cmd_render(host):
    sys.stdout.write(render_configscript(host))


def cmd_apply(host):
    cfg = render_configscript(host).rstrip("\n")
    V = hosts()[host]["values"]
    key = hosts()[host]["configScript"]
    lines = open(V).read().split("\n")
    ki = next(i for i, ln in enumerate(lines) if re.match(rf"^(\s*){re.escape(key)}:\s*\|", ln))
    key_indent = len(lines[ki]) - len(lines[ki].lstrip(" "))
    content_indent = key_indent + 2
    j = ki + 1
    while j < len(lines):
        ln = lines[j]
        if ln.strip() == "":
            j += 1
            continue
        if len(ln) - len(ln.lstrip(" ")) <= key_indent:
            break
        j += 1
    new = [(" " * content_indent + cl).rstrip() if cl.strip() else "" for cl in cfg.split("\n")]
    out = lines[: ki + 1] + new + lines[j:]
    open(V, "w").write("\n".join(out))
    print(
        f"applied generated {key} into {os.path.relpath(V, repo_root())} "
        f"(replaced lines {ki + 2}..{j}, {len(new)} new content lines)"
    )
    cmd_check(host)


def cloud_shape(clouds):
    """(kind, name, template-count) per cloud: the set-level drift signature."""
    out = []
    for c in clouds:
        kind = list(c)[0]
        body = list(c.values())[0]
        out.append(
            (
                kind,
                body.get("name") or body.get("fleet"),
                len(body.get("templates") or body.get("serverTemplates") or []),
            )
        )
    return out


def cmd_check(host, st=None):
    standalone = st is None
    if standalone:
        st = _stage.Stages()
    rendered = render_configscript(host)
    aliases = re.findall(r"(?:^|\s)[&*]id\d+\b", rendered)
    if aliases:
        st.fail(
            host,
            f"configScript emits {len(aliases)} YAML anchors/aliases; JCasC (SnakeYAML) "
            "caps aliases at 50 and the reload fails. The dumper must ignore_aliases",
        )
        if standalone:
            sys.exit(1)
        return
    gen = yaml.safe_load(detpl(rendered))["jenkins"]["clouds"]
    com = yaml.safe_load(detpl(committed_configscript(host)))["jenkins"]["clouds"]
    if cloud_shape(gen) != cloud_shape(com):
        st.fail(
            host, f"cloud set differs: generated {cloud_shape(gen)} committed {cloud_shape(com)}"
        )
        if standalone:
            sys.exit(1)
        return
    d = diff(com, gen)
    if d:
        st.fail(host, f"{len(d)} field diffs between catalog and committed configScript")
        st.echo("\n".join(d[:40]))
        if standalone:
            sys.exit(1)
        return
    st.ok(host, f"{hosts()[host]['configScript']} in sync with the catalog {cloud_shape(gen)}")


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="cdpctl clouds", description=__doc__)
    ap.add_argument("cmd", nargs="?", default="check", choices=("render", "apply", "check"))
    ap.add_argument(
        "host",
        nargs="?",
        choices=sorted(hosts()),
        help="catalog host; omitted = every catalog host (check only). The catalog "
        "covers IN-CLUSTER masters (JCasC clouds); EC2 masters deliver clouds via "
        "init.groovy.d and the runbook instead",
    )
    _stage.add_output_flags(ap)
    args = ap.parse_args(argv)
    mode = _stage.output_mode(args)
    if args.cmd == "check":
        # Every overlay in the catalog (or one host): a future in-cluster
        # master joins the gate by existing, with no CI edit to remember.
        st = _stage.Stages(quiet=(mode != "human"))
        targets = [args.host] if args.host else sorted(hosts())
        for host in targets:
            cmd_check(host, st)
        if mode == "json":
            print(json.dumps(st.envelope(hosts=targets), indent=2))
        elif mode == "llm":
            st.emit_llm()
        return st.exit_code()
    if args.host is None:
        ap.error(f"{args.cmd} needs an explicit host (one of: {', '.join(sorted(hosts()))})")
    {"render": cmd_render, "apply": cmd_apply}[args.cmd](args.host)
    return 0


if __name__ == "__main__":
    sys.exit(main())
