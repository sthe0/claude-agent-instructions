"""Mutants of the per-Core-version report and its verification tools: the record readers and the
core-version view (lib/telemetry/report.py), check-events and check-latency (scripts/telemetry.py),
the live-versus-transcript comparison (scripts/telemetry-compare.py), the synthetic driver and the
benchmark, and the first-line key the importer and the token collector share.

Each mutant is `{name, file, anchor, replacement, tests}`: `anchor` occurs exactly once in
`file` (a path under the repo root), `replacement` keeps the module importable, `tests` are
the pytest node ids that must go red. The catalogue test checks the anchors and the ids.
"""
from __future__ import annotations

_IMPORTER = "scripts/lib/telemetry/importer.py"
_K = "scripts/tests/test_telemetry_tokens.py::"
_I = "scripts/tests/test_telemetry_importer.py::"

MUTANTS = [
    {"name": "oversized_first_line_skipped", "file": _IMPORTER,
     "anchor": 'return raw if raw.endswith(b"\\n") or len(raw) == FIRST_LINE_MAX else None',
     "replacement": 'return raw if raw.endswith(b"\\n") else None',
     "tests": [_K + "test_oversized_first_line_transcript_is_collected",
               _I + "test_transcript_with_oversized_first_line_gets_transcript_events"]},
]
