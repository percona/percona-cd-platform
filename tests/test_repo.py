"""_repo primitives: root discovery and die()."""

from __future__ import annotations

import os

import pytest

from cdpctl import _repo


def test_repo_root_finds_this_checkout():
    root = _repo.repo_root()
    assert os.path.isfile(os.path.join(root, "justfile"))
    assert os.path.isdir(os.path.join(root, "terraform"))
    assert os.path.isdir(os.path.join(root, "src", "cdpctl"))


def test_die_raises_systemexit_with_code(capsys):
    with pytest.raises(SystemExit) as e:
        _repo.die("boom", 7)
    assert e.value.code == 7
    assert "boom" in capsys.readouterr().err
