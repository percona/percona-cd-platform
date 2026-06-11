"""conventions: rule regexes plus both directions of the scripts/ allowlist."""

from __future__ import annotations

import pathlib

from cdpctl import conventions


def test_banner_regex_accepts_only_exact_owner_lines():
    assert conventions.BANNER_RE.match("# Owner: platform")
    assert not conventions.BANNER_RE.match("## Owner: platform")
    assert not conventions.BANNER_RE.match("# Owner: Platform")
    assert not conventions.BANNER_RE.match("# Owner: platform  ")


def test_ticket_regex_matches_known_projects_only():
    assert conventions.TICKET_RE.search("see PS-11225 for details")
    assert conventions.TICKET_RE.search("PKG-1334")
    assert not conventions.TICKET_RE.search("K8SPSMDB-1603")
    assert not conventions.TICKET_RE.search("PS11225")


def test_team_arg_regex_anchored_to_assignment_lines():
    assert conventions.TEAM_ARG_RE.match('  team = "cloud-cd"').group(1) == "cloud-cd"
    assert not conventions.TEAM_ARG_RE.match('# the team = "cloud" tag is special')


def _patch_tracked(monkeypatch, paths: list[str]):
    out = "".join(f"scripts/{p}\n" for p in paths)
    monkeypatch.setattr(conventions.subprocess, "check_output", lambda *a, **kw: out)


def test_allowlist_accepts_exactly_the_frozen_set(monkeypatch):
    _patch_tracked(
        monkeypatch,
        ["README.md", "install-master-observability.sh", "karpenter-tests/00-namespace.yaml"],
    )
    assert conventions.check_scripts_allowlist(pathlib.Path(".")) == []


def test_allowlist_flags_a_new_loose_script(monkeypatch):
    _patch_tracked(
        monkeypatch,
        ["README.md", "install-master-observability.sh", "sneaky.py"],
    )
    errors = conventions.check_scripts_allowlist(pathlib.Path("."))
    assert len(errors) == 1
    assert "scripts/sneaky.py" in errors[0]
    assert "cdpctl subcommand" in errors[0]


def test_allowlist_flags_stale_entries_so_the_list_shrinks(monkeypatch):
    _patch_tracked(monkeypatch, ["README.md"])
    errors = conventions.check_scripts_allowlist(pathlib.Path("."))
    assert len(errors) == 1
    assert "install-master-observability.sh" in errors[0]
    assert "no longer exists" in errors[0]
