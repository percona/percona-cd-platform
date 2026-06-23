from typing import Optional, Union

from pydantic import BaseModel, ConfigDict, Field

from mcp_jenkins.jenkins.model.build import Build

ItemType = Union['Folder', 'MultiBranchProject', 'FreeStyleProject', 'Job', 'UnknownItem']


class AssignedLabel(BaseModel):
    name: str | None = None


class _ItemBase(BaseModel):
    model_config = ConfigDict(populate_by_name=True)

    class_: str = Field(..., alias='_class')
    name: str
    url: str
    fullname: str = Field(default=None, alias='fullName')

    def assigned_label_name(self) -> str | None:
        """The job's assigned agent label name, or None. Freestyle/matrix expose it; pipeline
        (WorkflowJob) keeps its agent label in the Jenkinsfile, so this is None for them."""
        label = getattr(self, 'assignedLabel', None)
        if label is None:
            return None
        return label.get('name') if isinstance(label, dict) else getattr(label, 'name', None)


class Job(_ItemBase):
    color: str
    lastBuild: Optional['Build'] = None
    assignedLabel: Optional['AssignedLabel'] = None


class FreeStyleProject(_ItemBase):
    color: str
    lastBuild: Optional['Build'] = None
    assignedLabel: Optional['AssignedLabel'] = None


class Folder(_ItemBase):
    jobs: list['ItemType']


class MultiBranchProject(_ItemBase):
    jobs: list['ItemType']
    lastBuild: Optional['Build'] = None


class UnknownItem(_ItemBase):
    model_config = ConfigDict(extra='allow')


def serialize_item(item: dict) -> ItemType:
    _class = item.get('_class', '')

    cls_map = {
        'Folder': Folder,
        'MultiBranchProject': MultiBranchProject,
        'FreeStyleProject': FreeStyleProject,
        'Job': Job,
    }
    target_cls = next((cls for name, cls in cls_map.items() if _class.endswith(name)), UnknownItem)

    if 'jobs' in item and isinstance(item['jobs'], list):
        item = {
            **item,
            'jobs': [serialize_item(job) if isinstance(job, dict) else job for job in item['jobs']],
        }

    return target_cls.model_validate(item)
