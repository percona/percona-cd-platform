"""_stage: row format, counters, exit semantics."""

from __future__ import annotations

import pytest

from cdpctl import _stage


def test_rows_and_counters(capsys):
    st = _stage.Stages()
    st.ok("alpha", "fine")
    st.fail("beta", "broken")
    st.skip("gamma", "later")
    out = capsys.readouterr().out
    # Non-TTY capture: no ANSI, exact two-space rows.
    assert "  PASS alpha fine\n" in out
    assert "  FAIL beta broken\n" in out
    assert "  SKIP gamma later\n" in out
    assert (st.passed, st.failed, st.skipped) == (1, 1, 1)
    assert st.exit_code() == 1


def test_exit_zero_without_failures():
    st = _stage.Stages()
    st.ok("only")
    st.skip("rest")
    assert st.exit_code() == 0


def test_reprint_issues_excludes_passes(capsys):
    st = _stage.Stages()
    st.ok("good")
    st.fail("bad", "why")
    capsys.readouterr()
    st.reprint_issues()
    out = capsys.readouterr().out
    assert "good" not in out
    assert "  FAIL bad why\n" in out


def test_require_exits_2_on_missing_tool():
    with pytest.raises(SystemExit) as e:
        _stage.require("definitely-not-a-real-binary-xyz")
    assert e.value.code == 2


def test_section_format(capsys):
    _stage.section("Summary")
    assert capsys.readouterr().out == "\n== Summary ==\n"


def test_render_table_kubectl_style():
    out = _stage.render_table(["master", "status"], [["ps3-k8s", "OK"], ["pg.cd", "MISSING"]])
    lines = out.splitlines()
    assert lines[0] == "MASTER   STATUS"
    assert lines[1] == "ps3-k8s  OK"
    assert lines[2] == "pg.cd    MISSING"


def test_emit_rows_llm_and_json(capsys):
    _stage.emit_rows(["a", "b"], [["x", 1]], "llm")
    assert capsys.readouterr().out == "x|1\n"
    _stage.emit_rows(["a", "b"], [["x", 1]], "json")
    assert '"a": "x"' in capsys.readouterr().out


def test_quiet_stages_record_without_printing(capsys):
    st = _stage.Stages(quiet=True)
    st.section("Hidden")
    st.ok("a")
    st.fail("b")
    st.echo("nope")
    assert capsys.readouterr().out == ""
    assert (st.passed, st.failed) == (1, 1)
    st.emit_llm()
    assert capsys.readouterr().out == "PASS|a|\nFAIL|b|\n"
