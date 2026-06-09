"""Contract tests between the Terraform instantiations and the handlers.

The moto suite proves handler behavior given env vars; these prove the WIRING
that delivers them: the env var names Terraform sets must be exactly the names
the handlers read, the configured handler entrypoint must exist in the packaged
file, and the runtime pin must match what the suite runs against. A typo in any
of these ships green tests and a silently misconfigured reaper.
"""

from __future__ import annotations

import pathlib
import re

import pytest

pytestmark = [pytest.mark.iam]

TF_DIR = pathlib.Path(__file__).resolve().parents[2]   # terraform/
LAMBDAS = TF_DIR / "lambdas"


def _tf(name: str) -> str:
    return (TF_DIR / name).read_text()


def _env_keys(tf_text: str) -> set[str]:
    block = re.search(r"environment_variables\s*=\s*\{(.*?)\}", tf_text, re.S)
    assert block, "environment_variables block not found"
    return set(re.findall(r"^\s*([A-Z_]+)\s*=", block.group(1), re.M))


def _handler_env_reads(subdir: str) -> set[str]:
    src = (LAMBDAS / subdir / "index.py").read_text()
    return set(re.findall(r'os\.environ\.get\(\s*"([A-Z_]+)"', src))


def test_volume_env_contract() -> None:
    tf_keys = _env_keys(_tf("volume-cleanup.tf"))
    handler_keys = _handler_env_reads("volume-cleanup")
    # Terraform must set only knobs the handler reads...
    assert tf_keys <= handler_keys, f"TF sets unread env: {tf_keys - handler_keys}"
    # ...and every knob except the test-only REGIONS override must be set.
    assert handler_keys - {"REGIONS"} <= tf_keys, f"handler knobs unset by TF: {handler_keys - {'REGIONS'} - tf_keys}"


def test_ec2_env_contract() -> None:
    tf_keys = _env_keys(_tf("ec2-cleanup.tf"))
    handler_keys = _handler_env_reads("ec2-cleanup")
    assert tf_keys <= handler_keys, f"TF sets unread env: {tf_keys - handler_keys}"
    assert handler_keys - {"REGIONS"} <= tf_keys, f"handler knobs unset by TF: {handler_keys - {'REGIONS'} - tf_keys}"


@pytest.mark.parametrize("tf_name,subdir", [
    ("volume-cleanup.tf", "volume-cleanup"),
    ("ec2-cleanup.tf", "ec2-cleanup"),
])
def test_handler_entrypoint_exists(tf_name: str, subdir: str) -> None:
    tf = _tf(tf_name)
    m = re.search(r'handler\s*=\s*"(\w+)\.(\w+)"', tf)
    assert m, f"handler not found in {tf_name}"
    module_name, func_name = m.groups()
    src_file = LAMBDAS / subdir / f"{module_name}.py"
    assert src_file.is_file(), f"{tf_name} points at {module_name}.py, which does not exist"
    assert re.search(rf"^def {func_name}\(", src_file.read_text(), re.M), (
        f"{src_file.name} has no function {func_name}"
    )
    # source_dir in TF must reference the same directory the file lives in.
    assert f"lambdas/{subdir}" in tf


def test_runtime_pin_matches_locals() -> None:
    locals_tf = _tf("locals.tf")
    m = re.search(r'cleanup_lambda_runtime\s*=\s*"(python3\.\d+)"', locals_tf)
    assert m, "cleanup_lambda_runtime not found in locals.tf"
    # Both instantiations must consume the shared pin, not hardcode their own.
    for name in ("volume-cleanup.tf", "ec2-cleanup.tf"):
        tf = _tf(name)
        assert "local.cleanup_lambda_runtime" in tf, f"{name} does not use the shared runtime pin"
        assert "python3." not in tf, f"{name} hardcodes a runtime"


def test_dry_run_locked_to_committed_state() -> None:
    """Both reapers were ARMED on 2026-06-09 after the dry-run bake-in (the
    volume dry run surfaced 12 orphans incl a 16 TB volume; EC2 ran clean
    cycles). This lock works in both directions: flipping a reaper's dry_run,
    either way, must update this expectation in the same PR, so the safety
    knob can never move silently."""
    expected = {"volume_cleanup": "false", "ec2_cleanup": "false"}
    locals_tf = _tf("locals.tf")
    for block, want in expected.items():
        m = re.search(rf'{block}\s*=\s*{{(.*?)}}', locals_tf, re.S)
        assert m, f"{block} params block not found in locals.tf"
        assert re.search(rf'dry_run\s*=\s*"{want}"', m.group(1)), (
            f"{block} dry_run is not \"{want}\"; update this lock in the same PR as the flip"
        )
