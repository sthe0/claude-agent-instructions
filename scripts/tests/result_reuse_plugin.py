"""pytest plugin of the suite gate: leave out the tests whose stored pass the gate decided to reuse.

Difficulty removed: the gate decides which tests a stored pass covers (``scripts/lib/result_store.py``) but
pytest has to be told not to run them. The gate loads this plugin with ``-p`` and names two files in the
environment:

    ``CORE_RESULT_REUSE_IDS``  one node id per line, the tests to leave out;
    ``CORE_RESULT_REUSE_OUT``  a path prefix; each pytest process writes the ids it actually left out to
                               ``<prefix>.<pid>`` (every xdist worker collects, so there is one file per
                               worker; the gate reads their union).

An id the gate named that this run does not collect (the test was removed, renamed or is out of
``select``) is simply not left out and not written, so the gate learns which of its hits really covered a
collected test. With neither variable set the plugin does nothing. Left-out tests are reported to pytest as
deselected, so the per-test report lists only the tests that ran; the gate adds the reused ones back.
"""
from __future__ import annotations

import os
from pathlib import Path

IDS_ENV = "CORE_RESULT_REUSE_IDS"
OUT_ENV = "CORE_RESULT_REUSE_OUT"


def reused_ids(path: str) -> frozenset[str]:
    return frozenset(line for line in Path(path).read_text(encoding="utf-8").splitlines() if line)


def pytest_collection_modifyitems(config, items):
    source, out = os.environ.get(IDS_ENV), os.environ.get(OUT_ENV)
    if not source or not out:
        return
    wanted = reused_ids(source)
    left_out = [item for item in items if item.nodeid in wanted]
    items[:] = [item for item in items if item.nodeid not in wanted]
    if left_out:
        config.hook.pytest_deselected(items=left_out)
    Path(f"{out}.{os.getpid()}").write_text("".join(f"{item.nodeid}\n" for item in left_out), encoding="utf-8")
