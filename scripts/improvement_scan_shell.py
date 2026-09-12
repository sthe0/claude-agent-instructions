#!/usr/bin/env python3
"""Subprocess-reaching helpers for improvement-scan.py's telemetry producer.

Isolated into its own module so improvement-scan.py's own source never
mentions `subprocess` — test_improvement_scan.py's
test_module_never_shells_out_or_reaches_the_network asserts the WHOLE module
is free of the transport-capable roots ast_purity.py names, and that
assertion predates this stage. The telemetry producer still needs to reuse
policy-scorecard.py's ledger upsert and record-experience.py's dedup search
rather than re-implementing either (CLAUDE.md's reuse-over-reinvent rule), so
the subprocess reach lives here instead, isolated and independently covered
by this module's own purity test (`impure_names(shell) <= {"subprocess"}`,
plus a check that only these two script names are ever invoked).
"""
from __future__ import annotations

import subprocess
import sys
from pathlib import Path
from typing import Iterable

SCRIPT_DIR = Path(__file__).resolve().parent
POLICY_SCORECARD = SCRIPT_DIR / "policy-scorecard.py"
RECORD_EXPERIENCE = SCRIPT_DIR / "record-experience.py"

_TIMEOUT_S = 60


def refresh_policy_ledger(
    days: int, *, ledger_path: "str | Path | None" = None
) -> "tuple[bool, str]":
    """Shell out to `policy-scorecard.py --ledger-only`, refreshing the
    on-disk ledger in place. Returns (ok, message) and never raises — a
    failed refresh must surface as a DEGRADED run to the caller rather than
    silently proceeding against a stale ledger.
    """
    cmd = [sys.executable, str(POLICY_SCORECARD), "--ledger-only", "--days", str(days)]
    if ledger_path is not None:
        cmd += ["--ledger", str(ledger_path)]
    try:
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=_TIMEOUT_S)
    except (OSError, subprocess.TimeoutExpired) as exc:
        return False, f"policy-scorecard subprocess failed to run: {exc}"
    if result.returncode != 0:
        return False, f"policy-scorecard exited {result.returncode}: {result.stderr.strip()}"
    return True, result.stderr.strip()


def search_experience(
    keywords: "Iterable[str]", *, scope: str = "global"
) -> "tuple[bool, bool, str]":
    """Shell out to `record-experience.py search`. Returns (ok, found, output).

    `ok=False` means the subprocess itself failed (missing interpreter,
    timeout, non-zero exit) — distinct from `found=False`, which means the
    search ran cleanly and reported "no analogous leaf found". Conflating the
    two would let a dedup check silently pass on a broken search subprocess.
    """
    cmd = [sys.executable, str(RECORD_EXPERIENCE), "search", "--scope", scope, *keywords]
    try:
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=_TIMEOUT_S)
    except (OSError, subprocess.TimeoutExpired) as exc:
        return False, False, f"record-experience search subprocess failed to run: {exc}"
    output = ((result.stdout or "") + (result.stderr or "")).strip()
    if result.returncode != 0:
        return False, False, output or f"record-experience search exited {result.returncode}"
    found = "no analogous" not in output
    return True, found, output
