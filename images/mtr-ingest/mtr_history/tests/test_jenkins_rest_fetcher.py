"""Tests for jenkins_rest_fetcher — the stdlib REST API replacement."""

from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from mtr_history.jenkins_rest_fetcher import (
    JenkinsFetchError,
    _normalize_detail,
    cli_version,
    download_junit_xmls,
    fetch_detail,
    fetch_history,
)


# --- fixtures ---------------------------------------------------------------

SAMPLE_API_BUILD = {
    "number": 1558,
    "url": "https://ps80.cd.percona.com/job/percona-server-8.0-pipeline-parallel-mtr/1558/",
    "timestamp": 1774523435614,
    "duration": 5885562,
    "result": "UNSTABLE",
    "displayName": "1558 Debug/oraclelinux:10/x86_64 (dlenev) ",
    "building": False,
    "actions": [
        {
            "_class": "hudson.model.ParametersAction",
            "parameters": [
                {"name": "GIT_REPO", "value": "https://github.com/dlenev/percona-server"},
                {"name": "BRANCH", "value": "ps-8.0-10448"},
                {"name": "DOCKER_OS", "value": "oraclelinux:10"},
                {"name": "ARCH", "value": "x86_64"},
                {"name": "CMAKE_BUILD_TYPE", "value": "Debug"},
                {"name": "FULL_MTR", "value": "yes"},
            ],
        },
        {
            "_class": "hudson.model.CauseAction",
            "causes": [
                {
                    "_class": "hudson.model.Cause$UserIdCause",
                    "shortDescription": "Started by user Dmitry Lenev",
                    "userId": "dlenev",
                    "userName": "Dmitry Lenev",
                }
            ],
        },
        {
            "_class": "hudson.plugins.git.util.BuildData",
            "remoteUrls": ["https://github.com/Percona-Lab/ps-build"],
            "lastBuiltRevision": {
                "SHA1": "abc123",
                "branch": [{"name": "refs/remotes/origin/8.0", "SHA1": "abc123"}],
            },
        },
    ],
    "artifacts": [
        {"fileName": "junit_WORKER_1.xml", "relativePath": "work/results/junit_WORKER_1.xml"},
        {"fileName": "junit_WORKER_2.xml", "relativePath": "work/results/junit_WORKER_2.xml"},
        {"fileName": "build.log.gz", "relativePath": "build.log.gz"},
    ],
}

SAMPLE_HISTORY_RESPONSE = {
    "builds": [
        {"number": 1559, "result": "FAILURE", "duration": 312000, "timestamp": 1744106286000},
        {"number": 1558, "result": "UNSTABLE", "duration": 5885562, "timestamp": 1774523435614},
        {"number": 1555, "result": "UNSTABLE", "duration": 7020000, "timestamp": 1774293833000},
    ]
}


# --- cli_version ------------------------------------------------------------

def test_cli_version():
    assert cli_version() == "rest-api/1.0"


# --- _normalize_detail ------------------------------------------------------

def test_normalize_detail_basic_fields():
    d = _normalize_detail(SAMPLE_API_BUILD)
    assert d["build"] == 1558
    assert d["timestamp_ms"] == 1774523435614
    assert d["duration_ms"] == 5885562
    assert d["result"] == "UNSTABLE"
    assert d["display_name"].strip() == "1558 Debug/oraclelinux:10/x86_64 (dlenev)"


def test_normalize_detail_parameters():
    d = _normalize_detail(SAMPLE_API_BUILD)
    params = {p["name"]: p["value"] for p in d["parameters"]}
    assert params["DOCKER_OS"] == "oraclelinux:10"
    assert params["ARCH"] == "x86_64"
    assert params["CMAKE_BUILD_TYPE"] == "Debug"
    assert params["GIT_REPO"] == "https://github.com/dlenev/percona-server"
    assert params["BRANCH"] == "ps-8.0-10448"


def test_normalize_detail_cause():
    d = _normalize_detail(SAMPLE_API_BUILD)
    assert d["cause"]["kind"] == "Manual"
    assert d["cause"]["user"] == "Dmitry Lenev"
    assert "Started by user" in d["cause"]["description"]


def test_normalize_detail_source():
    d = _normalize_detail(SAMPLE_API_BUILD)
    assert d["source"]["repo"] == "https://github.com/dlenev/percona-server"
    assert d["source"]["fork"] == "dlenev"
    assert d["source"]["branch"] == "ps-8.0-10448"
    assert d["source"]["sha"] is None


def test_normalize_detail_scm():
    d = _normalize_detail(SAMPLE_API_BUILD)
    assert len(d["scm"]) == 1
    assert d["scm"][0]["remote"] == "https://github.com/Percona-Lab/ps-build"
    assert d["scm"][0]["sha"] == "abc123"


def test_normalize_detail_unknown_cause():
    payload = {**SAMPLE_API_BUILD, "actions": []}
    d = _normalize_detail(payload)
    assert d["cause"]["kind"] == "Unknown"
    assert d["cause"]["user"] is None


def test_normalize_detail_timer_cause():
    payload = {
        **SAMPLE_API_BUILD,
        "actions": [
            {
                "_class": "hudson.model.CauseAction",
                "causes": [
                    {
                        "_class": "hudson.triggers.TimerTrigger$TimerTriggerCause",
                        "shortDescription": "Started by timer",
                    }
                ],
            }
        ],
    }
    d = _normalize_detail(payload)
    assert d["cause"]["kind"] == "Timer"


# --- fetch_history (mocked HTTP) -------------------------------------------

def test_fetch_history_returns_builds():
    with patch("mtr_history.jenkins_rest_fetcher._get_json", return_value=SAMPLE_HISTORY_RESPONSE):
        builds = fetch_history("https://ps80.cd.percona.com", "some-job", 3)
    assert len(builds) == 3
    assert builds[0]["number"] == 1559
    assert builds[1]["result"] == "UNSTABLE"


def test_fetch_history_empty():
    with patch("mtr_history.jenkins_rest_fetcher._get_json", return_value={"builds": []}):
        builds = fetch_history("https://ps80.cd.percona.com", "some-job", 10)
    assert builds == []


def test_fetch_history_error():
    with patch("mtr_history.jenkins_rest_fetcher._get_json", side_effect=JenkinsFetchError("boom")):
        with pytest.raises(JenkinsFetchError, match="boom"):
            fetch_history("https://ps80.cd.percona.com", "some-job", 10)


# --- fetch_detail (mocked HTTP) --------------------------------------------

def test_fetch_detail_normalizes():
    with patch("mtr_history.jenkins_rest_fetcher._get_json", return_value=SAMPLE_API_BUILD):
        d = fetch_detail("https://ps80.cd.percona.com", "some-job", 1558)
    assert d["build"] == 1558
    assert d["cause"]["kind"] == "Manual"


# --- download_junit_xmls (mocked HTTP) -------------------------------------

def test_download_junit_xmls_filters_and_downloads(tmp_path):
    artifact_response = {
        "artifacts": [
            {"fileName": "junit_WORKER_1.xml", "relativePath": "work/results/junit_WORKER_1.xml"},
            {"fileName": "junit_WORKER_2.xml", "relativePath": "work/results/junit_WORKER_2.xml"},
            {"fileName": "build.log.gz", "relativePath": "build.log.gz"},
        ]
    }

    def mock_get_json(url, username=None, token=None):
        return artifact_response

    def mock_download(url, dest, username=None, token=None):
        dest.write_text(f"<xml>{dest.name}</xml>")

    with patch("mtr_history.jenkins_rest_fetcher._get_json", side_effect=mock_get_json), \
         patch("mtr_history.jenkins_rest_fetcher._download_file", side_effect=mock_download):
        paths = download_junit_xmls("https://ps80.cd.percona.com", "some-job", 1558, tmp_path)

    assert len(paths) == 2
    assert all(p.name.startswith("junit_") for p in paths)
    assert paths == sorted(paths)


def test_download_junit_xmls_no_artifacts(tmp_path):
    with patch("mtr_history.jenkins_rest_fetcher._get_json", return_value={"artifacts": []}):
        paths = download_junit_xmls("https://ps80.cd.percona.com", "some-job", 1558, tmp_path)
    assert paths == []
