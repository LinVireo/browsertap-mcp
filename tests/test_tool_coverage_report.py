from __future__ import annotations

from scripts.tool_coverage_report import _offline_verified_tool_names


def test_offline_verification_does_not_count_tools_without_offline_evidence():
    evidence = {
        "live_only": ("tests/test_live_browser.py::test_live",),
        "mixed": (
            "tests/test_live_browser.py::test_live",
            "tests/test_offline.py::test_boundary",
        ),
        "offline": ("tests/test_offline.py::test_success",),
    }
    offline = {
        "tests/test_offline.py::test_boundary",
        "tests/test_offline.py::test_success",
    }
    passed = set(offline)

    assert _offline_verified_tool_names(evidence, offline, passed) == [
        "mixed",
        "offline",
    ]


def test_offline_verification_requires_every_offline_node_to_pass():
    evidence = {
        "tool": (
            "tests/test_offline.py::test_success",
            "tests/test_offline.py::test_boundary",
        )
    }
    offline = set(evidence["tool"])

    assert _offline_verified_tool_names(evidence, offline, {offline.pop()}) == []
