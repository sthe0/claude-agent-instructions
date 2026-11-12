"""Per-test report of a suite run, written as the run goes: ``CORE_TEST_REPORT=<path>`` turns it on.

Difficulty removed: a suite run recorded only its wall-clock and failed ids, so nothing told
which tests had finished when a run died, and nothing could be reused per test. The file is
JSON lines keyed by the exact pytest node id (the string ``--collect-only -q`` prints):

  {"kind": "collected", "nodeid": ...}                          once per collected test
  {"kind": "report", "nodeid": ..., "when": ..., "outcome": ...}  per pytest_runtest_logreport

Only the controlling process writes: without xdist it collects itself
(``pytest_collection_finish``); under xdist it does not collect, so the first worker's
``pytest_xdist_node_collection_finished`` gives the ids, and every worker's reports reach the
controller's ``pytest_runtest_logreport``. Each line is flushed as written, so a pytest killed
mid-run leaves the lines written so far; a file with no ``collected`` line means the run died
before collection finished. Registered from conftest.py; the variable is removed from the
environment once taken, so a pytest a test starts never writes into the same file.
"""
from __future__ import annotations

import json
import os
from typing import Iterable

import pytest

REPORT_ENV = "CORE_TEST_REPORT"
PLUGIN_NAME = "core-test-report"
COLLECTED, REPORT = "collected", "report"


class Reporter:
    def __init__(self):
        self._fh = None
        self._collected = False

    def pytest_configure(self, config):
        path = os.environ.get(REPORT_ENV)
        if path and not hasattr(config, "workerinput"):
            os.environ.pop(REPORT_ENV)
            self._fh = open(path, "w", encoding="utf-8")

    def _write(self, line: dict) -> None:
        self._fh.write(json.dumps(line) + "\n")
        self._fh.flush()

    def _write_collected(self, ids: Iterable[str]) -> None:
        if self._fh is None or self._collected:
            return
        self._collected = True
        for nodeid in ids:
            self._write({"kind": COLLECTED, "nodeid": nodeid})

    def pytest_collection_finish(self, session):
        self._write_collected(item.nodeid for item in session.items)

    @pytest.hookimpl(optionalhook=True)
    def pytest_xdist_node_collection_finished(self, node, ids):
        self._write_collected(ids)

    def pytest_runtest_logreport(self, report):
        if self._fh is not None:
            self._write({"kind": REPORT, "nodeid": report.nodeid, "when": report.when,
                         "outcome": report.outcome})

    def pytest_unconfigure(self, config):
        if self._fh is not None:
            self._fh.close()
            self._fh = None


def register(pluginmanager) -> None:
    if not pluginmanager.has_plugin(PLUGIN_NAME):
        pluginmanager.register(Reporter(), PLUGIN_NAME)


# ── reading a report ──────────────────────────────────────────────────────

def read_lines(path) -> list[dict]:
    """The report's lines; a torn last line (the writer was killed mid-write) is dropped."""
    lines = []
    with open(path, encoding="utf-8") as fh:
        for raw in fh:
            try:
                lines.append(json.loads(raw))
            except json.JSONDecodeError:
                continue
    return lines


def collected_ids(lines: Iterable[dict]) -> list[str]:
    return [line["nodeid"] for line in lines if line.get("kind") == COLLECTED]


def is_terminal(line: dict) -> bool:
    """A test has finished at its ``call`` report, or at a ``setup`` report that skipped or failed
    (pytest reports a skip only in setup, and a fixture error has no call phase)."""
    if line.get("kind") != REPORT:
        return False
    when, outcome = line.get("when"), line.get("outcome")
    return when == "call" or (when == "setup" and outcome in ("skipped", "failed"))


def finished_ids(lines: Iterable[dict]) -> set[str]:
    return {line["nodeid"] for line in lines if is_terminal(line)}


def outcomes(lines: Iterable[dict]) -> dict[str, str]:
    """The outcome of every collected test: ``failed`` (any phase failed), ``skipped``, ``passed``,
    or ``unfinished`` (collected, but no terminal report)."""
    lines = list(lines)
    seen: dict[str, set[str]] = {}
    for line in lines:
        if line.get("kind") == REPORT:
            seen.setdefault(line["nodeid"], set()).add(line.get("outcome"))
    finished = finished_ids(lines)
    result = {}
    for nodeid in collected_ids(lines):
        phases = seen.get(nodeid, set())
        if "failed" in phases:
            result[nodeid] = "failed"
        elif nodeid not in finished:
            result[nodeid] = "unfinished"
        else:
            result[nodeid] = "skipped" if "skipped" in phases else "passed"
    return result
