import importlib
import re
import sys

import pytest
import re2

# A pattern that backtracks for hours on Python re against a long run of '='. RE2 runs it in linear time.
CATASTROPHIC = '(=+)+$x'
_RE_FUNCTIONS = frozenset({'compile', 'search', 'match', 'fullmatch', 'finditer', 'findall', 'sub', 'subn', 'split'})
_GUARDED_MODULES = ('mcp_jenkins.jenkins.rest_client', 'mcp_jenkins.server')


class _RefusingRe:
    """Stand-in for the re module that fails at once when the catastrophic pattern reaches it."""

    def __init__(self, real) -> None:
        self._real = real

    def __getattr__(self, name: str) -> object:
        attribute = getattr(self._real, name)
        if name not in _RE_FUNCTIONS:
            return attribute

        def refuse(pattern: object, *args: object, **kwargs: object) -> object:
            if isinstance(pattern, str) and CATASTROPHIC in pattern:
                msg = f're.{name} got the catastrophic pattern: a caller pattern runs on the backtracking engine'
                raise AssertionError(msg)
            return attribute(pattern, *args, **kwargs)

        return refuse


@pytest.fixture
def re2_engine(monkeypatch):
    """Fail fast before any timing run when caller patterns do not compile with RE2.

    Every gateway module name bound to the re module is replaced by a stand-in that refuses the
    catastrophic pattern, and compile_user_regex must return an RE2 object.
    """
    importlib.import_module('mcp_jenkins.server')
    for name, module in list(sys.modules.items()):
        if module is None or not name.startswith(_GUARDED_MODULES):
            continue
        for attribute, value in list(vars(module).items()):
            if value is re:
                monkeypatch.setattr(module, attribute, _RefusingRe(re))
    from mcp_jenkins.jenkins.rest_client import compile_user_regex

    compiled = compile_user_regex(CATASTROPHIC)
    assert type(compiled) is type(re2.compile('a')), f'compile_user_regex returned {type(compiled)!r}, not RE2'
