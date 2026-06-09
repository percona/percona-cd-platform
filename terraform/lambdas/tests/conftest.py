"""Shared fixtures for the cleanup-Lambda tests.

Each handler is a standalone ``index.py`` under ``terraform/lambdas/<name>/``.
Both files are named ``index.py``, so a plain ``import index`` would collide;
we load each as a uniquely-named module via importlib.

The handlers read their env (DRY_RUN, MIN_AGE_HOURS, REGIONS, EKS_SKIP_PATTERN)
at invocation, so a test sets env with ``monkeypatch.setenv`` and then calls
``lambda_handler`` -- no reload needed.
"""

from __future__ import annotations

import importlib.util
import pathlib
import sys
from types import ModuleType

import pytest

# Importing the handlers must not litter the (zipped) Lambda source dirs with
# __pycache__/*.pyc -- data.archive_file would bundle them, making the package
# hash non-deterministic.
sys.dont_write_bytecode = True

LAMBDAS_DIR = pathlib.Path(__file__).resolve().parents[1]  # terraform/lambdas


def _load(mod_name: str, subdir: str) -> ModuleType:
    path = LAMBDAS_DIR / subdir / "index.py"
    spec = importlib.util.spec_from_file_location(mod_name, path)
    assert spec is not None and spec.loader is not None
    mod = importlib.util.module_from_spec(spec)
    sys.modules[mod_name] = mod
    spec.loader.exec_module(mod)
    return mod


@pytest.fixture(scope="session")
def volume_cleanup() -> ModuleType:
    return _load("volume_cleanup_index", "volume-cleanup")


@pytest.fixture(scope="session")
def ec2_cleanup() -> ModuleType:
    return _load("ec2_cleanup_index", "ec2-cleanup")


@pytest.fixture(autouse=True)
def _aws_env(monkeypatch: pytest.MonkeyPatch) -> None:
    """Dummy credentials for moto, and a clean slate for the handler env knobs."""
    monkeypatch.setenv("AWS_ACCESS_KEY_ID", "testing")
    monkeypatch.setenv("AWS_SECRET_ACCESS_KEY", "testing")
    monkeypatch.setenv("AWS_SECURITY_TOKEN", "testing")
    monkeypatch.setenv("AWS_SESSION_TOKEN", "testing")
    monkeypatch.setenv("AWS_DEFAULT_REGION", "us-east-1")
    for knob in ("DRY_RUN", "MIN_AGE_HOURS", "REGIONS", "EKS_SKIP_PATTERN"):
        monkeypatch.delenv(knob, raising=False)
