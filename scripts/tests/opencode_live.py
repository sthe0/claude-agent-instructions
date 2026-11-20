"""Shared helpers for tests that need the real OpenCode binary, a model credential, or a probe record.

A live test carries ``@opencode_live`` (binary) and, when it drives a real model, also
``@opencode_credential``. Each marker skips with a reason that starts with ``opencode live: binary``,
``opencode live: credential`` or ``opencode live: record`` so ``--require-live`` runs can tell a skip
from a failure. The binary is ``~/.opencode/bin/opencode`` unless ``AGENT_OPENCODE_BIN`` names another.
The credential is only ever an identifier: the environment variable ``AGENT_OPENCODE_CREDENTIAL_ID``
or the uncommitted file ``credential-id`` in the probe state dir; its value is never printed.
"""
import os

import pytest

from opencode_probe import core, harness, record

CREDENTIAL_ENV = "AGENT_OPENCODE_CREDENTIAL_ID"
CREDENTIAL_FILE = "credential-id"


def binary_problem():
    problem = core.binary_problem()
    if problem:
        return problem
    try:
        harness.ensure_pinned_version(core.opencode_bin())
    except harness.VersionMismatch as exc:
        return f"opencode live: binary {exc}"
    return None


def credential_problem():
    if os.environ.get(CREDENTIAL_ENV) or (core.state_dir() / CREDENTIAL_FILE).is_file():
        return None
    return f"opencode live: credential no {CREDENTIAL_ENV} and no {core.state_dir() / CREDENTIAL_FILE}"


def record_problem(phase_name):
    """Why the current tree has no record of the phase to compare with (binary checked first)."""
    problem = binary_problem()
    if problem:
        return problem
    phase = core.get_phase(phase_name)
    found = record.latest_record(core.state_dir(), phase_name, core.read_pin()["version"], core.phase_hash(phase))
    return None if found else f"opencode live: record none of phase {phase_name} for the current tree"


def latest_record(phase_name):
    """The latest record of the phase for the current tree as ``(path, record)``; skips the calling test when there is none."""
    problem = record_problem(phase_name)
    if problem:
        pytest.skip(problem)
    phase = core.get_phase(phase_name)
    return record.latest_record(core.state_dir(), phase_name, core.read_pin()["version"], core.phase_hash(phase))


def latest_case(phase_name, case_id):
    """The latest record's entry of one case; skips the calling test when there is none."""
    return latest_record(phase_name)[1]["cases"][case_id]


def require_binary():
    problem = binary_problem()
    if problem:
        pytest.skip(problem)


def require_credential():
    require_binary()
    problem = credential_problem()
    if problem:
        pytest.skip(problem)


opencode_live = pytest.mark.skipif(binary_problem() is not None, reason=binary_problem() or "")
opencode_credential = pytest.mark.skipif(
    binary_problem() is not None or credential_problem() is not None,
    reason=binary_problem() or credential_problem() or "")
