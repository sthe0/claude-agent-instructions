"""Mutants of PR 11: the hold measured from BEGIN IMMEDIATE's return (lib/telemetry/store.py)
and the checkpoint-pass bound a writable connection sets (lib/telemetry/store.py).

Each mutant is `{name, file, anchor, replacement, tests}`: `anchor` occurs exactly once in
`file` (a path under the repo root), `replacement` keeps the module importable, `tests` are
the pytest node ids that must go red. The catalogue test checks the anchors and the ids.

The hold's semantics: a queued BEGIN spends its busy wait inside sqlite3, before BEGIN
IMMEDIATE returns, so the wait on a second writer stays outside max_lock_hold_ms. The first
mutant moves the clock read to before BEGIN, folding the wait into the hold; the test that
simulates the wait on the fake clock inside the BEGIN statement kills it. The second mutant
drops the checkpoint-pass bound, returning every writable connection to SQLite's default
1000 pages: the verify greps `inner_bound_` in this file, and the negative control runs every
`inner_bound_*` mutant the harness lists, so the bound is discriminated by its own mutant,
not only asserted by the pragma test.
"""
from __future__ import annotations

_STORE = "scripts/lib/telemetry/store.py"
_S = "scripts/tests/test_telemetry_sync_scale.py::"
_EXCLUDES_WAIT = _S + "test_sync_end_max_lock_hold_excludes_the_wait_on_the_writer_lock"

MUTANTS = [
    {"name": "hold_measured_from_begin_return", "file": _STORE,
     "anchor": "    _set_busy(conn, deadline.busy_ms())\n    conn.execute(\"BEGIN IMMEDIATE\")\n"
              "    _hold[\"began\"] = _hold_clock()",
     "replacement": "    _set_busy(conn, deadline.busy_ms())\n    _hold[\"began\"] = _hold_clock()\n"
                    "    conn.execute(\"BEGIN IMMEDIATE\")",
     "tests": [_EXCLUDES_WAIT]},
    {"name": "inner_bound_checkpoint_pacing_dropped", "file": _STORE,
     "anchor": "        conn.execute(f\"PRAGMA wal_autocheckpoint={WAL_AUTOCHECKPOINT_PAGES}\")",
     "replacement": "        pass",
     "tests": [_S + "test_writable_connections_bound_the_checkpoint_pass"]},
]