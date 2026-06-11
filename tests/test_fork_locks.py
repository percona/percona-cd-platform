"""fork-locks: version ordering and release filtering (the bump gate)."""

from __future__ import annotations

from cdpctl.fork_locks import _manifest_field, latest_eligible, version_key


def test_version_key_orders_numerically_not_lexically():
    versions = ["103.percona.9", "103.percona.10", "103.percona.26"]
    assert max(versions, key=version_key) == "103.percona.26"
    assert sorted(versions, key=version_key) == [
        "103.percona.9",
        "103.percona.10",
        "103.percona.26",
    ]


def test_version_key_strict_newer_gate_semantics():
    cur, latest = "5.24.percona.4", "5.24.percona.3"
    # A lock manually pinned ahead of the newest release must not downgrade.
    assert max(cur, latest, key=version_key) != latest


def test_latest_eligible_filters_drafts_prereleases_and_upstream_tags():
    releases = [
        {"tag_name": "v103.percona.29", "draft": True, "prerelease": False},
        {"tag_name": "v103.percona.28", "draft": False, "prerelease": False},
        {"tag_name": "v103.percona.30", "draft": False, "prerelease": True},
        {"tag_name": "v104.0", "draft": False, "prerelease": False},  # upstream sync
    ]
    assert latest_eligible(releases, allow_prerelease=False) == "103.percona.28"
    assert latest_eligible(releases, allow_prerelease=True) == "103.percona.30"


def test_latest_eligible_empty_when_no_percona_tags():
    assert (
        latest_eligible([{"tag_name": "v104.0", "draft": False, "prerelease": False}], False) == ""
    )


def test_manifest_field_parses_and_ignores_cr():
    man = "Manifest-Version: 1.0\r\nShort-Name: ec2\r\nPlugin-Version: 5.24.percona.4\r\n"
    assert _manifest_field(man, "Short-Name") == "ec2"
    assert _manifest_field(man, "Plugin-Version") == "5.24.percona.4"
    assert _manifest_field(man, "Absent") == ""
