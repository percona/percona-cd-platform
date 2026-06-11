"""Shared primitives for cdpctl subcommands.

Import-time stdlib-only: the conventions gate runs in CI on a bare system
python via `PYTHONPATH=src python3 -m cdpctl conventions`, so nothing here may
import third-party packages at module level (load_yaml imports yaml lazily).
"""

from __future__ import annotations

import functools
import json
import os
import subprocess
import sys
import urllib.request


@functools.cache
def repo_root() -> str:
    """Locate the percona-cd-platform checkout to operate on.

    Prefer the repo the caller is standing in (a second clone is a valid
    target), fall back to the checkout this package was installed from
    (editable install invoked from outside any clone). The justfile +
    terraform/ pair is the marker that distinguishes this repo from an
    arbitrary git checkout.
    """

    def is_root(p: str) -> bool:
        return os.path.exists(os.path.join(p, "justfile")) and os.path.isdir(
            os.path.join(p, "terraform")
        )

    try:
        out = subprocess.run(
            ["git", "rev-parse", "--show-toplevel"], capture_output=True, text=True, timeout=10
        )
        top = out.stdout.strip()
        if out.returncode == 0 and top and is_root(top):
            return top
    except OSError:
        pass

    p = os.path.dirname(os.path.abspath(__file__))
    while True:
        if is_root(p):
            return p
        parent = os.path.dirname(p)
        if parent == p:
            die(
                "cdpctl: cannot locate the percona-cd-platform repo root "
                "(run from inside a checkout)",
                2,
            )
        p = parent


def die(msg: str, code: int = 1):
    print(msg, file=sys.stderr)
    raise SystemExit(code)


def http_json(url: str, headers: dict | None = None, timeout: int = 25):
    req = urllib.request.Request(url, headers=headers or {})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.load(r)


def load_yaml(path: str):
    import yaml  # function-local: keeps `import cdpctl._repo` stdlib-only

    with open(path, encoding="utf-8") as f:
        return yaml.safe_load(f)
