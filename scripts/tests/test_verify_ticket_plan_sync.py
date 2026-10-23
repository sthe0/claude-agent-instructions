"""verify-ticket-plan-sync.py: unit tests for the plan-sync comparator."""
from __future__ import annotations

import importlib.util
import tempfile
from pathlib import Path

SCRIPTS_DIR = Path(__file__).resolve().parent.parent
SYNC_SCRIPT = SCRIPTS_DIR / "verify-ticket-plan-sync.py"


def _load():
    spec = importlib.util.spec_from_file_location("verify_ticket_plan_sync", SYNC_SCRIPT)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def test_ok():
    mod = _load()
    with tempfile.TemporaryDirectory() as tmp:
        p = Path(tmp) / "plan.toml"
        p.write_bytes(b"[meta]\n")
        sha = mod.plan_file_sha256(str(p))
        marker = mod.extract_marker(f"posted\n{mod.format_marker(str(p), sha)}\n")
        assert mod.check(sha, marker, "plan.toml") == "OK"


def test_drift():
    mod = _load()
    with tempfile.TemporaryDirectory() as tmp:
        p = Path(tmp) / "plan.toml"
        p.write_bytes(b"[meta]\n")
        sha = mod.plan_file_sha256(str(p))
        stale_sha = "0" * 64 if sha != "0" * 64 else "1" * 64
        marker = mod.extract_marker(f"posted\n{mod.format_marker(str(p), stale_sha)}\n")
        assert mod.check(sha, marker, "plan.toml") == "DRIFT"


def test_no_plan_no_marker():
    mod = _load()
    assert mod.check("a" * 64, None, "plan.toml") == "NO-PLAN"


def test_no_plan_wrong_plan_name():
    """A marker for a different plan file must be NO-PLAN, not DRIFT or OK."""
    mod = _load()
    with tempfile.TemporaryDirectory() as tmp:
        p = Path(tmp) / "plan.toml"
        p.write_bytes(b"[meta]\n")
        sha = mod.plan_file_sha256(str(p))
        # Marker carries the correct sha but names a different plan file.
        other_marker_text = (
            f"<!-- agent-plan-sync: plan_sha256={sha} plan=other_plan.toml -->"
        )
        marker = mod.extract_marker(other_marker_text)
        assert mod.check(sha, marker, "plan.toml") == "NO-PLAN"


def test_selftest_passes():
    mod = _load()
    assert mod._selftest() is True
