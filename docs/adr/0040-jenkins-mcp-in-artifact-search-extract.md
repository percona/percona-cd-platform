<!-- Copyright (C) 2026 Percona LLC -->
# 0040: Jenkins MCP in-artifact search and extract (incl. tarballs)

**Status:** Accepted (2026-06-23)
**Related:** [ADR 0039](0039-jenkins-mcp-s3-export.md) (the server-side S3 export this reuses for large extracted members; `extract_archive_artifact` is a new consumer of `s3_export`), [ADR 0038](0038-jenkins-mcp-image-autodeploy.md) (the gateway's deploy path), [ADR 0024](0024-jenkins-fleet-ownership-boundary.md) (the "large artifacts to S3" ownership principle), [ADR 0012](0012-authentik-saml-oidc-bridge.md) / [ADR 0013](0013-push-from-masters-with-nginx-bearer.md) (the gateway's Authentik/Duo OIDC + single-credential posture this stays inside).

## Context

The jenkins-mcp gateway's read tools were exercised in a real investigation of an UNSTABLE ASAN parallel-MTR build (ps80 #10: 25 failures buried in 15,709 passes). The existing tools were all-or-nothing for artifacts: `get_build_artifact` returns the WHOLE artifact (buffered in memory), and there was no way to grep a log or pull a single file out of an archive. The most useful per-test logs live INSIDE tarballs (`work/results/<job>-test-mtr_logs-N.tar.gz`, one per worker), so triage meant downloading and unpacking a whole archive to read one log. (0.6.0 already added `get_build_failure_summary` to replace the ~4MB raw test report; this ADR covers the artifact-content side.)

The artifact bytes are attacker-influenceable: a Jenkins job can archive any file, including a crafted or malformed tarball. So an in-artifact tool reads adversarial input, which makes its resource and traversal safety the load-bearing design concern, not a footnote.

## Decision

Add three `read`-tier tools in `images/jenkins-mcp/src/mcp_jenkins/server/build_artifact_search.py`:
`grep_build_artifact` (grep a log, or a member inside a `.tar.gz` via `archive_member`), `list_archive_artifact` (list tarball members, glob-filtered), and `extract_archive_artifact` (pull one member: inline if small, else S3). All STREAM the artifact (reuse `rest_client.stream_build_artifact`) and are HARD-CAPPED at read time. Nothing is written to disk.

### 1. Stream and bound memory at read time, never post-hoc

A chunked line reader (`_bounded_line_reader`) reads `response.raw` (or a tar member) in fixed blocks, caps the in-flight line buffer at `_MAX_LINE_LEN` (emit-truncated-then-discard-until-newline), and stops at `_MAX_BYTES_SCANNED`, clamping each read to the remaining budget. A blob with no newline therefore never buffers past the line cap. Bytes are decoded UTF-8 with replacement.

### 2. Decompression-bomb guard on the GUNZIPPED stream

Gzip is decompressed by the code (`gzip.GzipFile`), wrapped in a byte-bounded `_LimitedReader(limit=_MAX_DECOMPRESSED_TOTAL)`, and fed to `tarfile.open(mode='r|')` (uncompressed streaming). Because the cap wraps the DECOMPRESSED stream, it bounds member content AND the PAX/GNU/sparse extended-header data tarfile reads internally. `_LimitedReader.read` clamps the requested size to the remaining budget BEFORE reading, so a single large read (tarfile reads `_block(tarinfo.size)` for extended headers, where `tarinfo.size` is attacker-controlled) cannot decompress past the cap into memory. A member-count cap (`_MAX_MEMBERS_SCANNED`) bounds work on a many-member archive, and `tf.members` is cleared each iteration so the TarInfo cache stays bounded in stream mode.

### 3. Tarball member safety

Member names are validated (`_safe_member_name`: reject absolute, `..`, backslash, control chars, double-quote) and must equal `info.name` exactly (tarfile `r|` stores names verbatim, so the equality check cannot be bypassed by normalization). Only regular files are read; symlink, hardlink, device, fifo, dir, and sparse members are rejected (`_is_real_file`). Per-member size is capped (`_MAX_MEMBER_BYTES`). `list` filters out unsafe names rather than returning them. Malformed gzip/tar (`tarfile.TarError`/`EOFError`/`OSError`/`zlib.error`) is normalized to a clean `ValueError` with no internal paths or tracebacks; a bomb-guard trip surfaces as `scan_limited` (distinct from a normal `truncated`).

### 4. Grep is literal by default; large extract goes to S3

`grep_build_artifact` substring-matches by default; regex is opt-in, length-capped, and `re.error`-sanitized. Python `re` has no timeout, so ReDoS blast radius is bounded by the byte/line/match caps rather than a watchdog. `extract_archive_artifact` returns a member inline (utf-8, or base64 for binary) up to 256 KiB, else streams it to S3 and returns a presigned URL via the [ADR 0039](0039-jenkins-mcp-s3-export.md) path (`s3_export.upload_bytes` + presign + the `mcp_export` audit line) — the member is read under `_MAX_MEMBER_BYTES`.

### 5. Multi-model adversarial security review gated the design

Because these tools read adversarial input, the security model was reviewed by three independent models (Opus 4.8 + Codex gpt-5.5 + GLM glm-5.2) against a shared threat-model brief, not just bench-tested. The first Codex pass found that the original post-hoc caps allowed unbounded buffering; the rewrite (gunzip `_LimitedReader` + `tarfile r|` + chunked reader) closed it. A later GLM pass found a HIGH the other reviewers missed — `_LimitedReader` enforced its cap AFTER the read, so an extended-header size field could decompress past the cap into memory before the check fired — fixed in 0.7.1 by clamping the read to the remaining budget first. The "at least one reviewer runs the runtime, and a confident model claim still needs verifying" rule applied: each fix carries a regression test, and the fixes were verified live in-pod.

### 6. Job arch/label filter (companion)

`query_items` gains a `label_pattern` that filters on a job's assigned agent label (where architecture lives, e.g. `docker-aarch64`) and surfaces the resolved `label`; the `get_items` tree now fetches `assignedLabel[name]`. This matches FREESTYLE/MATRIX jobs only: a PIPELINE (WorkflowJob) keeps its agent label inside the Jenkinsfile, which Jenkins does not expose as a queryable job field. The `query_items` patterns are length-capped and `re.error`-sanitized like the grep pattern.

## Consequences

**(+)** Targeted triage: a developer can grep or pull a single per-test log out of an MTR tarball in one call, without downloading and unpacking the whole archive or blowing the context budget.

**(+)** The read-only-Jenkins boundary, the single shared credential, and the OIDC/Duo posture are unchanged; the new tools add no write surface.

**(+)** Memory and CPU stay bounded against a malicious or malformed artifact: a decompression bomb, a no-newline blob, a path-traversal or symlink member, and an oversized member are all rejected or capped, verified by tests and a 3-model review.

**(-)** Regex (opt-in) has a residual ReDoS surface bounded only by the input caps, not a timeout; literal search is the default and recommended.

**(-)** `label_pattern` matches freestyle/matrix jobs only. The entire reachable fleet (ps80/psmdb/pxc/cloud) is currently pipeline, so the filter returns nothing today; it becomes useful if freestyle/matrix jobs (or other masters) come into scope.

## Acceptance criteria

- `list`/`extract`/`grep` work on a real MTR tarball (`work/results/<job>-test-mtr_logs-N.tar.gz`): list shows members, extract returns a member (inline or S3), grep returns matching lines from a member.
- A crafted tarball cannot escape or OOM: traversal/absolute/symlink/oversized members are rejected; a decompression bomb trips `_LimitedReader` without materializing the payload; `_LimitedReader.read(huge)` returns at most the remaining budget.
- A malformed archive returns a sanitized `ValueError`, not an internal traceback.
- `query_items(label_pattern=...)` filters freestyle/matrix jobs and surfaces `label`; an invalid or over-long pattern raises a clean `ValueError`.
- The jenkins-mcp test suite (incl. the bomb / traversal / clamp cases) and `just ci` pass.

## References

- Tools: [`images/jenkins-mcp/src/mcp_jenkins/server/build_artifact_search.py`](../../images/jenkins-mcp/src/mcp_jenkins/server/build_artifact_search.py)
- S3 reuse: [`images/jenkins-mcp/src/mcp_jenkins/core/s3_export.py`](../../images/jenkins-mcp/src/mcp_jenkins/core/s3_export.py) `upload_bytes` (see [ADR 0039](0039-jenkins-mcp-s3-export.md))
- Label filter: [`images/jenkins-mcp/src/mcp_jenkins/jenkins/rest_client.py`](../../images/jenkins-mcp/src/mcp_jenkins/jenkins/rest_client.py) `query_items`, [`images/jenkins-mcp/src/mcp_jenkins/jenkins/model/item.py`](../../images/jenkins-mcp/src/mcp_jenkins/jenkins/model/item.py) `assigned_label_name`
- Tests: [`images/jenkins-mcp/tests/test_server/test_build_artifact_search.py`](../../images/jenkins-mcp/tests/test_server/test_build_artifact_search.py)
