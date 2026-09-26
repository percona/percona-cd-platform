"""Every EBS StorageClass stamps PerconaKeep=True on the volumes it creates.

The volume reaper deletes an available EBS volume older than 24 hours unless it
carries PerconaKeep, so a detached PersistentVolume without the tag would be
deleted. The StorageClass is the only place the tag is applied: the EBS CSI
driver's extraVolumeTags leave it out because the driver also stamps them on
snapshots, where PerconaKeep would block the snapshot reaper.
"""

from __future__ import annotations

import re
from pathlib import Path

STORAGECLASSES = (
    Path(__file__).resolve().parents[3]
    / "resources/addons/storageclass-gp3/templates/storageclasses.yaml"
)
PERCONA_KEEP = re.compile(r'^\s+tagSpecification_\d+:\s*"PerconaKeep=True"\s*$', re.M)


def _ebs_storageclasses(text: str) -> dict[str, str]:
    classes = {}
    for doc in re.split(r"^---\s*$", text, flags=re.M):
        if not re.search(r"^kind:\s*StorageClass\s*$", doc, re.M):
            continue
        if not re.search(r"^provisioner:\s*ebs\.csi\.aws\.com\s*$", doc, re.M):
            continue
        name = re.search(r"^  name:\s*(\S+)", doc, re.M)
        assert name, "StorageClass without metadata.name"
        classes[name.group(1)] = doc
    return classes


def test_storageclasses_file_has_ebs_classes() -> None:
    assert len(_ebs_storageclasses(STORAGECLASSES.read_text())) >= 3


def test_every_ebs_storageclass_stamps_perconakeep() -> None:
    classes = _ebs_storageclasses(STORAGECLASSES.read_text())
    missing = sorted(name for name, doc in classes.items() if not PERCONA_KEEP.search(doc))
    assert not missing, f"EBS StorageClasses without PerconaKeep=True: {missing}"


def test_check_fails_when_a_class_drops_the_tag() -> None:
    mutated = STORAGECLASSES.read_text().replace('"PerconaKeep=True"', '"Other=1"', 1)
    classes = _ebs_storageclasses(mutated)
    assert any(not PERCONA_KEEP.search(doc) for doc in classes.values())
