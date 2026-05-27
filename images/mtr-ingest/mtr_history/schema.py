"""Pydantic v2 models for the per-build JSON artefact (schema v1).

See /home/percona/.claude/plans/fluffy-kindling-neumann.md — "Stage A" section.
The canonical artefact is one file per build at builds/<instance>_<job>_<n>.json.
"""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

SCHEMA_VERSION = 1


class Platform(BaseModel):
    model_config = ConfigDict(extra="forbid")
    docker_os: str              # "oraclelinux:10" — colon preserved here; hyphenated only for Prometheus labels
    arch: str                   # "x86_64"
    cmake_build_type: str       # "Debug" | "RelWithDebInfo"


class Source(BaseModel):
    model_config = ConfigDict(extra="forbid")
    fork: str | None            # "dlenev" — GitHub owner segment of `repo`
    repo: str                   # "https://github.com/dlenev/percona-server"
    branch: str                 # "ps-8.0-10448"
    sha: str | None             # always null in schema v1


class Cause(BaseModel):
    model_config = ConfigDict(extra="forbid")
    kind: str                   # "Manual" | "Upstream" | "Timer" | ...
    user: str | None
    description: str            # raw shortDescription from Jenkins


class ScmEntry(BaseModel):
    model_config = ConfigDict(extra="forbid")
    remote: str
    branch: str
    sha: str


class Summary(BaseModel):
    # "pass" is a Python reserved word; expose via alias.
    model_config = ConfigDict(extra="forbid", populate_by_name=True)
    pass_: int = Field(alias="pass")
    fail: int
    skip: int
    total: int
    duration_s: float


class TestResult(BaseModel):
    model_config = ConfigDict(extra="forbid")
    suite: str                                                     # "audit_log"
    name: str                                                      # "audit_log_charset"
    run_context: Literal["regular", "ci_fs", "ps_protocol", "unit_tests"]
    worker: str                                                    # "WORKER_2"
    big: bool                                                      # true if from -big XML
    status: Literal["pass", "fail", "skip"]
    time_s: float
    failure_message: str | None = None                             # null unless fail; ≤ 2 KB


class BackfillMeta(BaseModel):
    model_config = ConfigDict(extra="forbid")
    schema_version: int = SCHEMA_VERSION
    backfill_ts: str                                               # ISO 8601 UTC
    jenkins_cli_version: str
    junit_xml_files: list[str]
    warnings: list[str] = []
    error: str | None = None                                       # populated on FETCH_ERROR tombstones


class Build(BaseModel):
    model_config = ConfigDict(extra="forbid")
    build_number: int
    url: str
    timestamp: str                                                 # ISO 8601 UTC
    duration_ms: int
    result: str                                                    # SUCCESS | UNSTABLE | FAILURE | ABORTED | FETCH_ERROR
    display_name: str

    platform: Platform
    source: Source
    cause: Cause
    scm: list[ScmEntry]
    parameters: dict[str, str]

    summary: Summary
    tests: list[TestResult]

    backfill_meta: BackfillMeta
