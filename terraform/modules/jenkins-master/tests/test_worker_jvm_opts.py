"""Static assertions over the agent JVM options of the Jenkins worker clouds.

Every place that sets the JVM options of an agent (Hetzner templates, EC2
templates, EC2 Fleet and docker clouds, and the ps3 clouds catalog) carries
the flag that keeps Base64 decoding out of the AVX-512 stub.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[4]
FLAG = "-XX:-UseBASE64Intrinsics"
MASTERS = REPO / "resources" / "jenkins-masters"
CATALOG = REPO / "resources" / "jenkins" / "clouds-catalog"

# Each pattern captures one literal JVM option string.
LITERALS = [
    re.compile(r"^\s*jvm[oO]ptsMap\['[^']+'\]\s*=\s*'([^']*)'", re.M),
    re.compile(r"^\s*'([^']*)',\s*// String jvmopts", re.M),
    re.compile(r"^\s*'([^']*)',\s*// jvmopts", re.M),
    re.compile(r"^\s*22, SSH_CRED_ID,\s*\n\s*'([^']*)',", re.M),
    re.compile(r"^\s*jvm[oO]pts:\s*(.*)$", re.M),
]


def _config_files() -> list[Path]:
    names = r"(htz\.cloud\.groovy|cloud\.groovy(\.tftpl)?|ec2FleetCloud\.groovy|dockerFarmCloud\.groovy\.tftpl)$"
    files = [path for path in MASTERS.glob("*/init.groovy.d/*") if re.search(names, path.name)]
    files += sorted(CATALOG.rglob("*.yaml"))
    return sorted(files)


def _literals(path: Path) -> list[str]:
    text = path.read_text()
    return [match.group(1) for pattern in LITERALS for match in pattern.finditer(text)]


def test_every_agent_option_string_is_found() -> None:
    """The patterns still match the files, so the check below cannot pass empty."""
    found = sum(len(_literals(path)) for path in _config_files())
    assert found >= 70


@pytest.mark.parametrize("path", _config_files(), ids=lambda path: str(path.relative_to(REPO)))
def test_agent_jvm_decodes_base64_in_java_code(path: Path) -> None:
    missing = [value for value in _literals(path) if FLAG not in value]
    assert missing == [], f"agent JVM options without {FLAG}: {missing}"
