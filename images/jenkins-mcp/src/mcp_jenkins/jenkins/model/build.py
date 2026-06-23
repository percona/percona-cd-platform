from typing import Optional

from pydantic import BaseModel


class Artifact(BaseModel):
    fileName: str
    relativePath: str
    displayPath: str | None = None


class Build(BaseModel):
    number: int
    url: str

    timestamp: int = None
    duration: int = None
    estimatedDuration: int = None

    building: bool = None
    result: str | None = None

    nextBuild: Optional['Build'] = None
    previousBuild: Optional['Build'] = None


class BuildReplay(BaseModel):
    scripts: list[str]


class PipelineStage(BaseModel):
    id: str
    name: str
    status: str | None = None
    durationMillis: int | None = None
    startTimeMillis: int | None = None


class PipelineStages(BaseModel):
    id: str | None = None
    name: str | None = None
    status: str | None = None
    durationMillis: int | None = None
    stages: list[PipelineStage] = []


class ChangeSetItem(BaseModel):
    commitId: str | None = None
    author: str | None = None
    msg: str | None = None
    timestamp: int | None = None
    affectedPaths: list[str] = []
