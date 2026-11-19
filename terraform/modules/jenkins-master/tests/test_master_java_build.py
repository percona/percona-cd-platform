"""Static assertions over the controller JVM package in user-data.sh.tftpl.

The master image is locked to one Amazon Linux release, so a rebuilt master
installs the Java of that release. The template then takes the controller JVM
from a newer release that carries its security fixes.
"""

from __future__ import annotations

import re
from pathlib import Path

MODULE = Path(__file__).resolve().parents[1]
TEMPLATE = (MODULE / "user-data.sh.tftpl").read_text()
VARIABLES = (MODULE / "variables.tf").read_text()
MAIN = (MODULE / "main.tf").read_text()

# The first Amazon Linux 2023 release whose Corretto builds fix the Base64
# decode stub (21.0.13.1.2 and 17.0.21.1.2).
FIRST_FIXED_RELEASE = "2023.12.20261117"


def _line_index(pattern: str) -> int:
    lines = TEMPLATE.splitlines()
    hits = [index for index, line in enumerate(lines) if re.search(pattern, line)]
    assert len(hits) == 1, f"expected one line matching {pattern!r}, found {len(hits)}"
    return hits[0]


def test_master_takes_java_from_the_fixed_release() -> None:
    """The upgrade runs after the install and before Jenkins is installed."""
    install = _line_index(r"retry yum -y install \$\{java_package\}\s*$")
    upgrade = _line_index(r"dnf -y upgrade --releasever=\$\{java_releasever\}")
    jenkins = _line_index(r"retry yum -y install jenkins-")
    assert install < upgrade < jenkins


def test_java_upgrade_covers_every_installed_java_package() -> None:
    """A master with the full JDK package also has the headless one."""
    line = TEMPLATE.splitlines()[_line_index(r"dnf -y upgrade --releasever=")]
    assert "'${java_package}*'" in line


def test_java_upgrade_does_not_stop_the_bootstrap() -> None:
    """The JVM flag still protects a master whose upgrade could not run."""
    line = TEMPLATE.splitlines()[_line_index(r"dnf -y upgrade --releasever=")]
    assert re.search(r"\|\|\s*echo \"WARN: ", line)


def test_java_release_default_carries_the_fix() -> None:
    block = re.search(r'variable "java_releasever" \{(.*?)\n\}', VARIABLES, flags=re.S)
    assert block, "the module declares java_releasever"
    default = re.search(r'default\s*=\s*"([^"]*)"', block.group(1))
    assert default and default.group(1) >= FIRST_FIXED_RELEASE


def test_template_receives_the_java_release() -> None:
    assert re.search(r"java_releasever\s*=\s*var\.java_releasever", MAIN)
