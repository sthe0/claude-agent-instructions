"""Stage 4 (R8a) of convergence-levers-r8-r9-r10-r2: measurement-only judge
instrumentation. Two things are pinned here:

  * lib/judge_ledger.py's `call()` and `decided()` gain a `prompt_chars` field
    -- the length of the stdin prompt subprocess_runner sent the judge model --
    written for every judge via that one chokepoint, and left `None` on a
    `decided` row where no prompt was ever built (a `stage="budget"` skip).
  * samples/judge-latency/field_inputs.py, a NEW sampler that measures REAL
    field latency for feedback_signal/binary_ask/deferring_disposition by
    reusing the exact input-extraction/prefilter logic the live hooks use
    (imported, never re-implemented), writing metadata-only rows -- no prompt
    text, no hash of it -- to a redirected ledger so the live one stays clean.

Imports of the two touched-but-pre-existing modules (`lib.judge_ledger`,
`agentctl.advisor`) and of the brand-new `field_inputs` module are all placed
INSIDE each test function, not at module level, so these six tests FAIL
individually against the pre-lever tree rather than making the whole file
error at collection.
"""
from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path

import pytest

SCRIPTS_DIR = Path(__file__).resolve().parent.parent
FIELD_INPUTS_DIR = SCRIPTS_DIR.parent / "samples" / "judge-latency"


def _load_hook(filename: str):
    name = filename.replace("-", "_").removesuffix(".py")
    existing = sys.modules.get(name)
    if existing is not None:
        return existing
    spec = importlib.util.spec_from_file_location(name, SCRIPTS_DIR / filename)
    mod = importlib.util.module_from_spec(spec)
    sys.modules[name] = mod
    spec.loader.exec_module(mod)
    return mod


def _load_field_inputs():
    """Load samples/judge-latency/field_inputs.py, reusing a prior load from
    sys.modules -- this is the NEW file this stage adds, so on the pre-lever
    tree this raises FileNotFoundError, a clean per-test failure rather than a
    collection-time error (the spec/module objects are constructed fine; only
    exec_module touches the missing file)."""
    name = "field_inputs"
    existing = sys.modules.get(name)
    if existing is not None:
        return existing
    spec = importlib.util.spec_from_file_location(name, FIELD_INPUTS_DIR / "field_inputs.py")
    mod = importlib.util.module_from_spec(spec)
    sys.modules[name] = mod
    spec.loader.exec_module(mod)
    return mod


def test_judge_call_row_records_prompt_chars():
    from lib import judge_ledger

    judge_ledger.hook_start("field_latency_test")
    judge_ledger.call(
        "binary_ask", timed_out=False, duration=0.42, returncode=0, prompt_chars=137,
    )
    records = judge_ledger.read_records()
    call_rows = [r for r in records if r.get("kind") == "call"]
    assert call_rows, "expected at least one call row"
    assert call_rows[-1]["prompt_chars"] == 137


def test_decided_row_records_prompt_chars():
    from lib import judge_ledger

    judge_ledger.hook_start("field_latency_test")
    judge_ledger.decided(
        "binary_ask", stage="call", verdict=True, reason="", duration=0.2, prompt_chars=456,
    )
    judge_ledger.decided(
        "binary_ask", stage="budget", verdict=False,
        reason="budget exhausted before call (fail-open)",
    )
    records = judge_ledger.read_records()
    decided_rows = [r for r in records if r.get("kind") == "decided"]
    assert len(decided_rows) == 2
    call_stage, budget_stage = decided_rows
    assert call_stage["stage"] == "call"
    assert call_stage["prompt_chars"] == 456
    assert budget_stage["stage"] == "budget"
    assert budget_stage["prompt_chars"] is None


def test_field_sample_carries_no_input_text():
    import hashlib

    field_inputs = _load_field_inputs()
    marker = "the user privately said this session's secret prompt text 12345"

    row = field_inputs.build_row(
        "binary_ask", marker, prompt_chars=len(marker), duration=1.23,
        timed_out=False, verdict=True,
    )
    assert set(row) == {"judge", "prompt_chars", "duration", "timed_out", "verdict"}

    serialized = json.dumps(row)
    assert marker not in serialized
    assert hashlib.sha256(marker.encode()).hexdigest() not in serialized
    assert hashlib.md5(marker.encode()).hexdigest() not in serialized
    assert row["prompt_chars"] == len(marker)
    assert row["judge"] == "binary_ask"
    assert row["verdict"] is True
    assert row["timed_out"] is False


def test_field_sampler_uses_hook_input_builders():
    field_inputs = _load_field_inputs()

    import si_feedback_detect
    from agentctl import advisor
    from lib import ask_text

    # Bound by NAME to the production symbols -- identity, not a re-implemented
    # lookalike that merely behaves the same on today's inputs.
    assert field_inputs.strip_injected_context is si_feedback_detect.strip_injected_context
    assert field_inputs.find_signals is si_feedback_detect.find_signals
    assert field_inputs.binary_ask_prefilter is advisor.binary_ask_prefilter
    assert field_inputs.question_texts is ask_text.question_texts
    assert field_inputs.option_texts is ask_text.option_texts
    assert field_inputs.question_stems is ask_text.question_stems

    turn_end_mod = field_inputs._load_hook("hook-turn-end-gate.py")
    assert field_inputs.assistant_text_of is turn_end_mod._assistant_text_of

    deferring_mod = field_inputs._load_hook("hook-deferring-disposition-gate.py")
    assert field_inputs.deferring_prefilter is deferring_mod._prefilter


def test_field_sampler_redirects_judge_ledger():
    import os

    from lib import judge_ledger as live_judge_ledger

    ledger_before = live_judge_ledger.ledger_path()
    # Force a FRESH exec of field_inputs.py's module body, regardless of
    # whether an earlier test in this file already cached it: the per-test
    # _isolate_judge_ledger fixture resets AGENTCTL_JUDGE_LEDGER before every
    # test, including this one, so only a real re-exec (not a cache hit)
    # proves THIS test's redirect rather than reading a stale assertion that
    # happened to pass only when this test ran first.
    sys.modules.pop("field_inputs", None)
    field_inputs = _load_field_inputs()

    redirected = str(field_inputs.SCRATCH / "field-inputs-judge-ledger.jsonl")
    assert os.environ["AGENTCTL_JUDGE_LEDGER"] == redirected
    assert str(live_judge_ledger.ledger_path()) == redirected
    assert str(ledger_before) != redirected

    live_judge_ledger.hook_start("field_inputs_redirect_probe")
    live_judge_ledger.call(
        "binary_ask", timed_out=False, duration=0.01, returncode=0, prompt_chars=1,
    )
    assert not ledger_before.exists() or "field_inputs_redirect_probe" not in ledger_before.read_text()
    assert "field_inputs_redirect_probe" in live_judge_ledger.ledger_path().read_text()


def test_field_sample_has_min_rows_and_censoring_shape():
    from lib import judge_ledger

    field_inputs = _load_field_inputs()

    # -- censoring shape: a call that hits the uncensored ceiling is recorded
    # timed_out=True at exactly the ceiling duration, never silently dropped.
    def _fake_ceiling_judge(prompt, runner, *, enabled=True, timeout=None, remaining=None, ceiling=None, runtime_host=None):
        judge_ledger.call(
            "fake_judge", timed_out=True, duration=timeout, returncode=None,
            prompt_chars=len(prompt),
        )
        judge_ledger.decided(
            "fake_judge", stage="call", verdict=False,
            reason="judge timed out (fail-open)", timed_out=True, duration=timeout,
            prompt_chars=len(prompt),
        )
        return False, "judge timed out (fail-open)"

    judge_ledger.hook_start("field_latency_test")
    row = field_inputs.run_judge_once("fake_judge", _fake_ceiling_judge, "a real-looking field input")
    assert row["timed_out"] is True
    assert row["duration"] == field_inputs.TIMEOUT_S
    assert row["verdict"] is False

    # -- min rows: target is fixed, `n`/`available` reflect the actual draw,
    # and a pool smaller than target yields every available item, not a
    # padded-out target-sized list.
    small_pool = [f"input {i}" for i in range(5)]
    summary = field_inputs.sample_judge(
        "fake_judge", lambda: small_pool, _fake_ceiling_judge,
    )
    assert summary["target"] == field_inputs.TARGET_N
    assert summary["available"] == 5
    assert summary["n"] == 5
    assert len(summary["rows"]) == 5

    # A pool at or above target draws exactly target items, deterministically.
    large_pool = [f"input {i}" for i in range(50)]
    drawn_once = field_inputs.draw_sample(large_pool)
    drawn_again = field_inputs.draw_sample(large_pool)
    assert len(drawn_once) == field_inputs.TARGET_N
    assert drawn_once == drawn_again
