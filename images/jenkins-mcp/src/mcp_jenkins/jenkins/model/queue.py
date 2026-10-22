from typing import Optional

from pydantic import BaseModel


class Queue(BaseModel):
    discoverableItems: list
    items: list['QueueItem']


class QueueItem(BaseModel):
    id: int
    inQueueSince: int
    url: str
    why: str | None

    task: 'QueueItemTask'

    # Waiting items carry the flags; a left item (started or cancelled) carries executable/cancelled.
    blocked: bool | None = None
    buildable: bool | None = None
    stuck: bool | None = None
    cancelled: bool | None = None
    executable: Optional['QueueExecutable'] = None


class QueueExecutable(BaseModel):
    number: int | None = None
    url: str | None = None


class QueueItemTask(BaseModel):
    fullDisplayName: str = None
    name: str = None
    url: str = None
