# Agent-cloud catalog

Source of truth for the agent clouds of in-cluster Jenkins controllers
([ADR 0029](../../../docs/adr/0029-fleet-agent-cloud-templatization.md)).

| File | Role |
|------|------|
| `catalog.yaml` | Shared templates, device maps, defaults. Hand-maintained. |
| `masters/<host>.yaml` | Per-controller overlay: which templates, labels, caps, regions. Hand-maintained. |

The controller values carry the rendered JCasC (`ps3-clouds` in
`../master/instances/ps3-k8s/values.yaml`). That block is generated, never edit it
by hand.

## Change a template

```bash
$EDITOR resources/jenkins/clouds-catalog/masters/ps3.yaml
scripts/render-clouds.py apply ps3     # regenerate the ps3-clouds block
just clouds-render-check               # CI drift gate, read-only
```

Merge, then sync `jenkins-ps3-k8s` in ArgoCD inside a window
([ADR 0025](../../../docs/adr/0025-singleton-controller-rollout-gating.md)). The
config-reload sidecar applies it without a pod restart.

## Rules

- `jenkins.clouds` is applied as a whole list, so every cloud of a controller is
  rendered into one configScript.
- Keep template and cloud names stable. Agents and the Hetzner template error
  tracker key on them.
