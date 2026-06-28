# Documentation conventions (docs/)

Scoped instructions for `docs/` (ADRs and runbooks). The repo-root `CLAUDE.md`
carries the platform-wide rules; the global no-em-dash / semicolons-sparingly
style applies here too. These are guidance, not a gated check.

## ADRs (`docs/adr/`)

- **One file per decision**, `NNNN-kebab-title.md`, `NNNN` zero-padded and
  monotonically increasing. Title line `# NNNN - Title` (a hyphen, not an em
  dash).
- **Header = terse cross-reference lines**, one per relationship, in this order
  when present: `**Status:**`, `**Amends:**`, `**Related:**`,
  `**Superseded by:**` / `**Partially reverses:**`, `**Amended by:**`. A
  cross-ref is a pointer with a one-clause why, not an explanation:
  `**Amended by:** [ADR 00NN](00NN-slug.md) (the MNGs moved to Graviton)`. The
  detail lives in the linked ADR.
- **Never stack two lines for the same relationship.** If an ADR is amended a
  second time, EDIT the existing `**Amended by:**` line to list both refs; do
  not add a second `**Amended by:**` line. Two `Amended by` lines that both name
  the same ADR is the noise to avoid.
- **Amend, do not rewrite.** When a later ADR changes a past decision, add an
  `**Amended by:**` pointer to the OLD ADR and record the change in the NEW one.
  The old ADR's body stays as the historical record (its superseded values are
  the "pre-migration record"); do not gut it or restate the new decision inside
  it.
- **Body sections:** `## Context`, `## Decision`, `## Consequences`; add
  `## Rollout` / `## Verification` only when they carry real content.
- **No dense paragraphs.** Break any multi-item content (a per-node-group
  rollout, a list of changes, several findings) into bullets. One idea per
  paragraph, a blank line between. A wall of text with no newlines and five
  facts crammed in is the anti-pattern.
- **State the decision and its rationale, not the play-by-play.** Cut the "too
  much info": ephemeral ops detail, a restatement of the diff, and timeline
  narrative belong in the PR or a runbook, not the ADR.

## Runbooks (`docs/runbooks/`)

- Present tense, imperative steps, copy-pasteable commands, terse. A gate or
  check belongs as a command the reader runs, not a paragraph describing it.
- Same no-tombstones and dates-only-when-load-bearing rules as below.

## Shared

- **No em dashes** (use commas, colons, or parentheses). Semicolons sparingly.
- **Dates only when load-bearing** (an incident that explains a sizing choice).
  Keep them out of cross-references.
- **No tombstones.** State current behavior in the present tense; git history
  carries what was removed. An ADR is the exception that PROVES the rule: it
  records a point-in-time decision, so its body stays as written and later
  change is captured by an `**Amended by:**` pointer, never by editing the body.
