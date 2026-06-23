# jenkins-mcp S3 exports

The jenkins-mcp gateway can stream a build log or artifact to a private S3 bucket and hand back
a short-lived presigned download URL, via the `export_build_log` and `export_build_artifact` MCP
tools. This runbook covers what the bucket is, the bearer-URL risk, and how to operate and verify
the path. Design: [ADR 0039](../adr/0039-jenkins-mcp-s3-export.md).

## What runs

| Piece | Where |
|---|---|
| Tools `export_build_log` / `export_build_artifact` | `images/jenkins-mcp/src/mcp_jenkins/server/build_export.py` |
| Stream + presign helper | `images/jenkins-mcp/src/mcp_jenkins/core/s3_export.py` |
| Bucket `percona-ci-platform-jenkins-mcp-exports` (private, SSE-S3, 7-day expiry) | `terraform/jenkins-mcp-exports.tf` |
| IAM (Put/Get/Abort/Delete on the bucket objects) | `terraform/iam-jenkins-mcp.tf` |
| Pod Identity association (ns + SA `jenkins-mcp`) | `terraform/pod-identity.tf` |
| Env `MCP_S3_BUCKET` / `MCP_AWS_REGION` / `MCP_S3_PRESIGN_TTL` | `resources/addons/jenkins-mcp/{values.yaml,templates/deployment.yaml}` |

## The bearer-URL risk (tell users)

The returned `url` is a bearer capability: anyone who has it can download the object with no
authentication until it expires. The window is bounded, not eliminated:

- TTL ceiling 6h (`MCP_S3_PRESIGN_TTL`, default 21600, capped in code).
- The URL is signed with the pod's temporary Pod Identity credentials, so it can expire EARLIER
  than 6h when those credentials rotate. Effective validity is min(TTL, remaining credential
  lifetime). A "my link died early" report is this, not a bug.
- Unguessable uuid4 key, fully private bucket, 7-day lifecycle delete.

Treat a presigned URL like a password: do not paste it where it should not be readable.

## Operate

- Change the link lifetime: set `s3.presignTtl` (seconds, 1 to 21600) in
  `resources/addons/jenkins-mcp/values.yaml` and let the addon roll. Values above 21600 are capped
  to 21600 in code.
- Disable exports: there is no separate flag; the tools require `MCP_S3_BUCKET`. Dropping the env
  (remove `s3.exportsBucket`) makes the tools error clearly at call time, the rest of the gateway
  is unaffected.
- The bucket auto-empties on a 7-day lifecycle, no manual cleanup is needed. A failed streaming
  upload has its multipart parts aborted after 1 day.

## Verify

Confirm the pod can reach S3 (Pod Identity), the bucket posture, and the audit line.

```sh
# Pod Identity: the pod can write+read the bucket (run from inside the pod, 0.4.0+ has boto3).
kubectl -n jenkins-mcp exec deploy/jenkins-mcp -- python -c \
  "import boto3,os;b=os.environ['MCP_S3_BUCKET'];c=boto3.client('s3');\
c.put_object(Bucket=b,Key='_probe',Body=b'x');\
print(c.get_object(Bucket=b,Key='_probe')['Body'].read());\
c.delete_object(Bucket=b,Key='_probe')"

# Bucket posture (operator shell with AWS creds).
aws s3api get-bucket-encryption --bucket percona-ci-platform-jenkins-mcp-exports
aws s3api get-public-access-block --bucket percona-ci-platform-jenkins-mcp-exports
aws s3api get-bucket-lifecycle-configuration --bucket percona-ci-platform-jenkins-mcp-exports

# Anonymous GET must be denied.
curl -s -o /dev/null -w '%{http_code}\n' \
  https://percona-ci-platform-jenkins-mcp-exports.s3.us-east-1.amazonaws.com/_probe   # -> 403
```

Audit (Grafana Loki), who exported what and to where (never the URL):

```
{namespace="jenkins-mcp"} | json | event="mcp_export"
```

Each line carries `sub`, `tool`, `master`, `fullname`, `number`, and the S3 `key`. The tool call
itself is also logged by the audit middleware (`{namespace="jenkins-mcp"} | json | tool="export_build_log"`);
the presigned URL is a return value and is never logged.
