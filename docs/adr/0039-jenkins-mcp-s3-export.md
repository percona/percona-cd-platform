<!-- Copyright (C) 2026 Percona LLC -->
# 0039: Jenkins MCP server-side S3 export for large logs and artifacts

**Status:** Accepted (2026-06-23)
**Related:** [ADR 0038](0038-jenkins-mcp-image-autodeploy.md) (the same gateway's deploy path; this is the first ADR to record a jenkins-mcp runtime capability), [ADR 0024](0024-jenkins-fleet-ownership-boundary.md) (the "large artifacts to S3" ownership principle), [ADR 0033](0033-s3-gateway-endpoint-policy-iam-governed.md) (allow S3 at the VPC gateway endpoint, govern at IAM, which this object-scoped policy does), [ADR 0013](0013-push-from-masters-with-nginx-bearer.md) and [ADR 0012](0012-authentik-saml-oidc-bridge.md) (the gateway's single-credential and Authentik/Duo OIDC posture this builds on).

## Context

The jenkins-mcp gateway (`resources/addons/jenkins-mcp/`) is a token-free, OIDC-authenticated read-only window onto the Jenkins fleet. Its existing read tools return content inline in the MCP response: `get_build_console_output` returns log text, `get_build_artifact` returns the artifact bytes (base64 for binary). For LARGE content that has two failure modes:

1. A multi-megabyte console log or a large artifact blows the model's context budget, or is simply too big to carry through the chat channel.
2. `get_build_artifact` reads the whole artifact into memory (`response.content`), so a large artifact risks OOM-killing the 256Mi pod.

Developers asked to be able to DOWNLOAD a full log or artifact, not just view a slice.

## Decision

Add two read tools, `export_build_log` and `export_build_artifact`, that fetch the content server-side, STREAM it to a private S3 bucket, and return a short-lived presigned GET URL. The payload never traverses the MCP response and never fully buffers in the pod.

### 1. Stream, never buffer

The export path opens a streaming GET against Jenkins (`stream=True`) and pipes `response.raw` straight into `boto3` `upload_fileobj` single-threaded (`TransferConfig(use_threads=False)`). For a non-seekable stream this reads one ~8MB multipart chunk at a time, so peak memory is bounded by the chunk size regardless of payload size (a multi-GB artifact streams in roughly 8MB). `response.raw.decode_content = True` ensures a gzip-encoded upstream is stored decoded. After upload, the stored object size is checked against the bytes read and the upstream `Content-Length`; on a mismatch the object is deleted and an error is raised, so a truncated upload is never presigned. Validated against real S3 (a 20MB stream uploaded with an 8MB peak read).

### 2. Read tier, with a bounded bearer-URL window

Both tools are tagged `read`: any authenticated Authentik/Duo user can call them, because the Jenkins read is exactly the one the inline tools already allow, and only the transport changes. The new exposure is that the returned presigned URL is a bearer capability anyone can use, with no auth, until it expires. That window is bounded by a 6-hour TTL ceiling (`MCP_S3_PRESIGN_TTL`, default 21600, capped in code), an unguessable uuid4 key per export, a fully private bucket (presigned access only), and a 7-day lifecycle expiry. The URL is signed with the pod's temporary Pod Identity credentials, so its effective validity is min(TTL, remaining credential lifetime): a 6h link can expire earlier when those credentials rotate. That is documented in the tool output and the runbook rather than worked around with long-lived keys.

### 3. Private bucket, SSE-S3, lifecycle expiry (not Object Lock)

A new bucket `percona-ci-platform-jenkins-mcp-exports` (`terraform/jenkins-mcp-exports.tf`): all public access blocked, BucketOwnerEnforced, SSE-S3 (AES256), a 7-day expiry lifecycle, and a DenyInsecureTransport bucket policy. SSE-S3 rather than a CMK, because a no-auth presigned GET carries no KMS grant and SSE-S3 keeps the download working with no per-key policy. Lifecycle expiry is the retention mechanism, not Object Lock, which broke Loki PutObject on this account (repo CLAUDE.md gotcha 9). No versioning: keys are uuid-unique and never overwritten.

### 4. Object-scoped IAM via Pod Identity

The pod gets its first AWS access through a new EKS Pod Identity association (`terraform/pod-identity.tf`) bound to (cluster, namespace `jenkins-mcp`, SA `jenkins-mcp`), attaching a policy (`terraform/iam-jenkins-mcp.tf`) scoped to `s3:PutObject`, `s3:GetObject`, `s3:AbortMultipartUpload`, and `s3:DeleteObject` on the one bucket's objects only (Put to stream in, Get to presign, Abort and Delete to clean up a failed or truncated upload). This is the "govern at IAM" half of [ADR 0033](0033-s3-gateway-endpoint-policy-iam-governed.md). `automountServiceAccountToken` stays false: Pod Identity serves credentials over the agent endpoint, not the projected SA token.

### 5. Per-export audit of the key, never the URL

The audit middleware already logs every tool call's identity and arguments to Loki, and never logs return values, so the presigned URL never reaches the logs. Each successful export additionally emits an `mcp_export` audit line carrying the S3 key (where the export went), so "who exported what to where" is answerable without ever recording the bearer URL.

## Consequences

**(+)** Developers can download a full log or artifact of any size through a signed link, without the payload crossing the chat channel or buffering in the pod.

**(+)** The read-only-Jenkins boundary, the single shared credential, and the OIDC/Duo posture are unchanged; the only new trust surface is the bounded, audited, auto-expiring presigned URL.

**(+)** Streaming makes the pod's memory independent of artifact size, which also removes the large `get_build_artifact` OOM risk on the export path.

**(-)** A presigned URL is a bearer capability for its lifetime: anyone it is forwarded to can download with no auth. Mitigated by the short TTL, the unguessable key, the private and expiring bucket, and the audit line; a user must still treat the link as sensitive.

**(-)** A 6h link can expire before 6h when the pod's temporary credentials rotate. Accepted and documented in the tool output and runbook. Guaranteeing a full 6h would require long-lived static keys, which the no-static-keys posture rejects.

## Acceptance criteria

- `export_build_log` and `export_build_artifact` return a working presigned URL that downloads the full content, and the bytes do not appear in the MCP response.
- A large artifact exports with pod memory staying well under 256Mi (streaming, not buffering).
- The bucket is private (anonymous GET 403), SSE-S3, 7-day expiry; a tampered presigned URL is rejected.
- An export emits an `mcp_export` Loki line with the S3 key and never the URL.
- `just ci` and the jenkins-mcp test suite pass.

## References

- Tools: [`images/jenkins-mcp/src/mcp_jenkins/server/build_export.py`](../../images/jenkins-mcp/src/mcp_jenkins/server/build_export.py)
- Stream + presign helper: [`images/jenkins-mcp/src/mcp_jenkins/core/s3_export.py`](../../images/jenkins-mcp/src/mcp_jenkins/core/s3_export.py)
- Bucket: `terraform/jenkins-mcp-exports.tf`; IAM: `terraform/iam-jenkins-mcp.tf`; association: `terraform/pod-identity.tf`
- Operator procedure: [docs/runbooks/jenkins-mcp-exports.md](../runbooks/jenkins-mcp-exports.md)
