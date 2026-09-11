"""Pytest hooks used by the canonical release-suite runner."""
from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

from scripts import evidence_manifest as E


def pytest_addoption(parser):
    group = parser.getgroup("BTAP release evidence")
    group.addoption("--btap-evidence-mode", choices=("offline", "live"))
    group.addoption("--btap-evidence-phase", choices=("collection", "execution"))
    group.addoption("--btap-evidence-output")


class _EvidenceRecorder:
    def __init__(self, config):
        self.config = config
        self.all_tests = []
        self.collection_reports = []
        self.deselected = []
        self.reports = []
        self.source_before = E.source_identity()

    @pytest.hookimpl(hookwrapper=True, tryfirst=True)
    def pytest_collection_modifyitems(self, session, config, items):
        # Capture before -m/-k or any later plugin can remove items. A subset
        # must not become its own definition of the expected whole suite.
        self.all_tests = [
            {"nodeid": item.nodeid, "live": item.get_closest_marker("live") is not None}
            for item in items
        ]
        yield
        for item in items:
            item.user_properties.append(("btap_nodeid", item.nodeid))

    def pytest_deselected(self, items):
        self.deselected.extend(item.nodeid for item in items)

    def pytest_collectreport(self, report):
        self.collection_reports.append({"nodeid": report.nodeid, "outcome": report.outcome})

    def pytest_runtest_logreport(self, report):
        self.reports.append({
            "nodeid": report.nodeid, "when": report.when, "outcome": report.outcome,
        })

    @pytest.hookimpl(trylast=True)
    def pytest_sessionfinish(self, session, exitstatus):
        config = self.config
        receipt = {
            "schema_version": 1,
            "mode": config.getoption("btap_evidence_mode"),
            "phase": config.getoption("btap_evidence_phase"),
            "command": [sys.executable, "-m", "pytest", *config.invocation_params.args],
            "targets": list(config.args),
            "keyword": config.option.keyword,
            "markexpr": config.option.markexpr,
            "invoked_from_root": (
                Path(config.invocation_params.dir).resolve() == E.ROOT.resolve()
                and Path(config.rootpath).resolve() == E.ROOT.resolve()
            ),
            "all_tests": self.all_tests,
            "collection_reports": self.collection_reports,
            "selected_nodeids": [item.nodeid for item in session.items],
            "deselected_nodeids": self.deselected,
            "reports": self.reports,
            "exit_code": int(exitstatus),
            "source_before": self.source_before,
            "source_after": E.source_identity(),
        }
        output = E.ROOT / config.getoption("btap_evidence_output")
        output.parent.mkdir(parents=True, exist_ok=True)
        output.write_bytes((json.dumps(receipt, indent=2, sort_keys=True) + "\n").encode("utf-8"))


def pytest_configure(config):
    if not all(config.getoption(name) for name in (
        "btap_evidence_mode", "btap_evidence_phase", "btap_evidence_output"
    )):
        raise pytest.UsageError("all BTAP release evidence options are required")
    config.pluginmanager.register(_EvidenceRecorder(config), "btap-release-recorder")
