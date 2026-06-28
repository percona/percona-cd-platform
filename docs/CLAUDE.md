# Documentation conventions (docs/)

Scoped to `docs/` (ADRs, runbooks). The repo-root `CLAUDE.md` has the platform
rules; the global no-em-dash, sparse-semicolon style applies here too. Guidance,
not a gate.

## ADRs (`docs/adr/`)

- One decision per file, `NNNN-kebab-title.md`. Title `# NNNN - Title` (hyphen, not an em dash).
- Header cross-refs, in order, one line each: `Status`, `Amends`, `Related`, `Superseded by` / `Partially reverses`, `Amended by`. Each is a pointer plus a one-clause why; the detail lives in the linked ADR.
- One line per relationship. Amended again? Edit the existing `Amended by` line to add the ref; never stack a second one.
- Amend, do not rewrite. A later ADR adds an `Amended by` pointer; the old body stays as the record (its superseded values are the "before" state).
- Body: `Context`, `Decision`, `Consequences` (add `Rollout` / `Verification` only when they carry real content). State the decision and its rationale, not the play-by-play.
- No dense paragraphs. Bullets for any multi-item content; one idea per paragraph.

## Runbooks (`docs/runbooks/`)

- Present tense, imperative steps, copy-pasteable commands, terse. A check is a command the reader runs, not a paragraph describing it.

## Everywhere

- No em dashes (commas, colons, parens). Semicolons sparingly.
- Dates only when load-bearing; keep them out of cross-refs.
- No tombstones: present tense, git history holds what was removed. The ADR body is the one exception, a point-in-time record changed only by an `Amended by` pointer.
