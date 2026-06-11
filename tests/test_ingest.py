"""ingest: dynamic master enumeration replaces the stale hardcoded list."""

from __future__ import annotations

from cdpctl.alloy import enumerate_masters
from cdpctl.ingest import build_records


def test_masters_come_from_repo_source_of_truth():
    labels = {m["label"] for m in enumerate_masters()}
    assert "ps3-k8s" in labels  # in-cluster controller, post-migration label
    assert "ps3.cd" not in labels  # the decommissioned EC2 master must not linger


def test_build_records_one_row_per_master_with_nulls():
    records = build_records({"pg.cd": "1"}, {}, {}, {})
    by = {r["master"]: r for r in records}
    assert by["pg.cd"]["mimir_series"] == "1"
    assert by["pg.cd"]["loki_lines"] is None
    assert len(records) == len(enumerate_masters())
