"""clouds: structural diff helpers plus the real catalog as a fixture."""

from __future__ import annotations

import yaml

from cdpctl.clouds import (
    cloud_shape,
    committed_configscript,
    detpl,
    diff,
    render_configscript,
)


def test_diff_reports_nested_field_changes():
    a = {"x": {"y": 1}, "list": [1, 2]}
    b = {"x": {"y": 2}, "list": [1, 2]}
    assert diff(a, b) == [".x.y: 1 != 2"]


def test_diff_reports_missing_keys_and_length():
    assert diff({"a": 1}, {"b": 1}) == [".a: only-A", ".b: only-B"]
    assert diff([1], [1, 2]) == [": len 1!=2"]


def test_cloud_shape_signature():
    clouds = [
        {"hetzner": {"name": "ps3-htz", "serverTemplates": [{}, {}]}},
        {"amazonEC2": {"name": "AWS-Dev b", "templates": [{}]}},
        {"eC2Fleet": {"fleet": "arm", "name": None}},
    ]
    assert cloud_shape(clouds) == [
        ("hetzner", "ps3-htz", 2),
        ("amazonEC2", "AWS-Dev b", 1),
        ("eC2Fleet", "arm", 0),
    ]


def test_rendered_ps3_matches_committed_catalog():
    """The drift gate's core equivalence, on the real repo files."""
    gen = yaml.safe_load(detpl(render_configscript("ps3")))["jenkins"]["clouds"]
    com = yaml.safe_load(detpl(committed_configscript("ps3")))["jenkins"]["clouds"]
    assert cloud_shape(gen) == cloud_shape(com)
    assert diff(com, gen) == []
