<!-- Copyright (C) 2026 Percona LLC -->
# 0030 — Account cleanup reapers in OpenTofu via a reusable scheduled-lambda module

**Status:** Accepted (2026-06-09)
**Related:** [ADR 0024](0024-jenkins-fleet-ownership-boundary.md) (everything the account runs should be reconstructible from code, not hand-deployed; this brings the last two manually-deployed reapers under that principle), [ADR 0027](0027-baked-jenkins-controller-image.md) (sibling "scoped GHA-OIDC role + immutable build + Trivy-gated supply chain" governance, and the `terraform/modules/github-oidc-role` caller-owns-the-policy philosophy this module mirrors).

## Context

Two cleanup reapers keep `percona-dev-admin` from accreting cost: a daily reaper of unattached (`available`) EBS volumes, and a five-minute reaper of untagged EC2 instances that also deletes orphan `eksctl-<cluster>-cluster` CloudFormation stacks. Both are the enforcement behind the account's two mandatory cleanup tags (`iit-billing-tag`, `PerconaKeep=True`; see CLAUDE.md gotcha 7). Both were **deployed manually as CloudFormation with inline handler code**: no tests, hardcoded role names, over-broad IAM, and an execution-role trust that named `lambda`, `ec2`, and `logs` as principals. The handlers had real correctness holes too: the volume reaper filtered on `tag:Name=*`, so it silently skipped untagged volumes (two untagged 30 TB gp3 orphans ran for days) and crashed outright on a volume with no tags.

These are the highest-blast-radius automations in the account: one is allowed to call `DeleteVolume` and `TerminateInstances` unattended, the other `DeleteStack`. Leaving them as untested console-deployed CloudFormation is exactly the black box [ADR 0024](0024-jenkins-fleet-ownership-boundary.md) wants gone. The reapers should be code in this repo: reviewable, testable, least-privilege, and armed by a reviewed edit rather than a console click.

## Decision

Move both reapers into this repo as OpenTofu, behind one reusable module, with typed and tested Python handlers. Merged as `#113` + `#117`.

### 1. A reusable `scheduled-lambda` module owns the wiring

`terraform/modules/scheduled-lambda` encodes the shape every reaper shares so a caller only thinks about the workload's destructive permissions: an `archive_file` zip (`__pycache__`/`*.pyc` excluded so the package hash stays deterministic) with `source_code_hash`; an EventBridge **Rule** plus target plus an `aws_lambda_permission` that always carries `source_arn` (the rule ARN); an explicit log group with retention created before the first invocation; `reserved_concurrent_executions = 1` so a destructive reaper can never overlap itself; and an execution role whose trust is `lambda.amazonaws.com` **only**. The module attaches just the AWS-managed `AWSLambdaBasicExecutionRole` (CloudWatch Logs); the **caller** owns the destructive permissions as an `aws_iam_policy_document` passed in as `permissions_policy_json`. That split mirrors the `github-oidc-role` module philosophy: the module owns the easy-to-get-wrong wiring, the instantiation file owns the workload's scoped grants.

### 2. Legacy EventBridge Rule, chosen over Scheduler deliberately

For a plain `rate()`/`cron()` trigger, EventBridge Scheduler buys nothing and adds a second IAM role and trust surface (the scheduler's own invoke role). The Rule plus `aws_lambda_permission(source_arn)` is the smaller surface, matches the rest of the repo, and the `source_arn` on the invoke permission is what satisfies the Trivy AWS-0067 control (without it any principal in the events service could invoke the function).

### 3. Handlers re-implemented as typed `python3.14`, env read at invocation

Reading env at invocation (not import) means a Terraform `DRY_RUN` flip is a plain apply and tests can pin values deterministically. The **volume reaper** evaluates every `available` volume regardless of tags, tolerates an empty/`None` tag set, honors a `MIN_AGE_HOURS` floor so a freshly created volume survives a build, keeps the `PerconaKeep` and case-insensitive `do not remove` opt-outs, and isolates failures per volume and per region so one undeletable volume or one broken region never aborts the sweep. The **EC2 reaper** treats a numeric `iit-billing-tag` as a unix-epoch expiry (so tagged-but-expired instances are reaped too), spares EKS clusters matching `EKS_SKIP_PATTERN`, and deletes orphan `eksctl-<cluster>-cluster` stacks. Two safety upgrades over the originals: the CloudFormation stack-tag protection probe **fails closed** (any error while determining whether a stack protects its cluster protects the cluster), and `delete_eks_stack` **re-reads the stack tags immediately before `DeleteStack`** as a TOCTOU guard against a billing tag added during the scan. The Cirrus CI auto-tag path was dropped entirely (Cirrus CI shut down 2026-06-01, so its stragglers are now reaped) along with its `ec2:CreateTags` grant.

### 4. IAM scoped as tightly as the APIs allow, residual risk documented

`Describe*` actions take `Resource: "*"` (no resource-level scoping exists). `TerminateInstances` is scoped to account instance ARNs with **no tag condition**, because the eligibility test compares an epoch tag against the clock and IAM cannot do that comparison. CloudFormation actions are scoped to `stack/eksctl-*-cluster/*`. The three `DELETE_FAILED` remediation actions (`RevokeSecurityGroupIngress`, `DisassociateRouteTable`, `DeleteRoute`) remain `Resource: "*"` because those APIs take physical IDs (`sg-`/`rtb-`); safety there rests on handler provenance (the handler only invokes them for resources of an `eksctl-*-cluster` stack it is already deleting). This residual risk is accepted and documented in `ec2-cleanup.tf`. Because each function is deployed once in the default-provider region while its handler sweeps regions in code, no policy may carry an `aws:RequestedRegion` condition.

### 5. Tested, and gated in CI

41 `moto`/`freezegun` tests cover the handlers and include **Terraform-to-handler contract tests**: env-name wiring (the names the `.tf` sets are the names the handler reads), handler entrypoint existence, the shared runtime pin matching `locals.tf`, and an assertion that the shipped config sets `dry_run = "true"`. They run as a credential-free `lambda-pytest` CI job.

### 6. Rollout: ship enabled, dry-run, arm by a reviewed edit

Schedules ship `ENABLED` with `DRY_RUN = "true"`. Arming a reaper is flipping its `dry_run` to `"false"` in the `locals.tf` "Cleanup Lambda parameters" block (a reviewed edit, not a CLI `-var`) after the dry-run logs look right. The legacy CloudFormation stacks were decommissioned ahead of this work; the volume stack's shared schedule rule was retained because two PMM cleanup functions share it.

## Consequences

- **(+)** The two highest-blast-radius automations in the account are now reviewable, least-privilege, tested code, reconstructible per [ADR 0024](0024-jenkins-fleet-ownership-boundary.md); the inline-code/hardcoded-role/over-broad-trust CloudFormation black box is gone.
- **(+)** The known volume-reaper bugs are fixed and locked by tests: untagged and no-tag volumes are evaluated safely, a `MIN_AGE_HOURS` floor protects fresh volumes, and the new fail-closed CFN probe plus TOCTOU re-read make the EC2 reaper harder to misfire than the original.
- **(+)** A reusable module makes the next scheduled reaper a thin caller (one policy document plus knobs); the `source_arn`-always and concurrency-1 controls are built in, so a future reaper cannot ship the AWS-0067 hole or overlap itself.
- **(−)** Account reaping is **paused** between the legacy decommission and the first apply-plus-arm; until each reaper's `dry_run` is flipped it observes and reports but deletes nothing. Re-arming is the explicit gate.
- **(−)** The reapers' own resources carry `PerconaKeep`/`iit-billing-tag` via the root default tags so they cannot self-reap, but each function lives in a single region while its handler sweeps regions in code, so no `aws:RequestedRegion` guard can defend the policies; their region-spanning scope is intrinsic.
- **(−)** The `DELETE_FAILED` remediation grants stay `Resource: "*"` (the APIs take physical IDs); that residual risk is accepted, documented, and bounded by handler provenance rather than by IAM.

## Alternatives considered

- **Keep the manual CloudFormation reapers.** Untested, hardcoded role names, over-broad IAM, and a three-service trust; invisible to code review and the source of the silent untagged-volume miss. Exactly the black box [ADR 0024](0024-jenkins-fleet-ownership-boundary.md) retires. Rejected.
- **EventBridge Scheduler instead of a Rule.** Adds a second IAM role and trust surface for a plain `rate()` trigger with no benefit, and would still need the AWS-0067-style scoping. Rejected for the larger surface.
- **Module composes the destructive policy.** Rejected: the `ec2:DeleteVolume` / `ec2:TerminateInstances` / `cloudformation:DeleteStack` grants have workload-specific resource and condition shapes, so they belong in the caller (the `github-oidc-role` split), leaving the module pure wiring.
- **Bundle bytecode in the deployment package.** Rejected: `.pyc` embeds source mtimes and an interpreter version, making the package hash non-deterministic and risking a wrong-runtime artifact; `__pycache__`/`*.pyc` are excluded from the zip.
- **Arm by CLI `-var` or console.** Rejected: arming a destructive reaper must be a reviewed, version-controlled edit in `locals.tf`, not an out-of-band flag or click.

## Verification

- `terraform/modules/scheduled-lambda/main.tf`: the invoke `aws_lambda_permission` always sets `source_arn` (AWS-0067); the role trust is `lambda.amazonaws.com` only; `reserved_concurrent_executions` defaults to 1; the log group is explicit with retention and the function `depends_on` it.
- `terraform/volume-cleanup.tf` and `ec2-cleanup.tf`: each caller supplies its own `permissions_policy_json`; `TerminateInstances` is scoped to account instance ARNs with no tag condition; CloudFormation actions are scoped to `stack/eksctl-*-cluster/*`; no `aws:RequestedRegion` condition appears.
- `locals.tf` "Cleanup Lambda parameters": both reapers ship `dry_run = "true"` and a single `cleanup_lambda_runtime` pin.
- The `lambda-pytest` CI job runs the 41 `moto`/`freezegun` tests, including the Terraform-to-handler contract tests (`test_terraform_contract.py`) that assert env-name wiring, entrypoint existence, the runtime pin, and `dry_run` shipping `"true"`.
- `just ci` passes (fmt, validate, Trivy, kubeconform).
