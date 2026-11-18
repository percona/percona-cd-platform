"""Static assertions over the Jenkins JVM arguments in user-data.sh.tftpl.

The systemd override written there holds the fixed arguments that every
master of this module starts its Jenkins JVM with.
"""

from __future__ import annotations

import re
from pathlib import Path

TEMPLATE = Path(__file__).resolve().parents[1] / "user-data.sh.tftpl"


def _java_opts() -> list[str]:
    lines = re.findall(
        r'^\s*Environment="JAVA_OPTS=(.*)"$', TEMPLATE.read_text(), flags=re.M
    )
    assert len(lines) == 1, "the template sets JAVA_OPTS exactly once"
    return lines[0].split()


def test_master_jvm_decodes_base64_in_java_code() -> None:
    """The master JVM never uses the AVX-512 Base64 stub. The flag sits after
    the per-master arguments, so a per-master value cannot turn the stub on."""
    opts = _java_opts()
    assert opts.count("-XX:-UseBASE64Intrinsics") == 1
    per_master = next(
        index for index, opt in enumerate(opts) if "jvm_extra_opts" in opt
    )
    assert opts.index("-XX:-UseBASE64Intrinsics") > per_master
