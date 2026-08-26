"""Stage 3: shared transcript-cost accounting — dedup, price weighting, tolerance.

The two rules this module exists to hold in one place (GitHub issue #144) are
exactly the two a second reader would otherwise rediscover the hard way, so both
are pinned here against a real mutation: drop the ``message.id`` dedup and the
first test fails; sum token COUNTS instead of pricing them and the weighting
tests fail.
"""
from __future__ import annotations

import datetime as dt
import importlib.util
import json
import sys
from pathlib import Path

SCRIPTS_DIR = Path(__file__).resolve().parents[1]
if str(SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPTS_DIR))

from lib import transcript_cost as tc  # noqa: E402

MTOK = 1_000_000
UTC = dt.timezone.utc


def _load_cost_report():
    """cost-report.py by path — hyphenated filename, the repo's usual idiom."""
    path = SCRIPTS_DIR / "cost-report.py"
    spec = importlib.util.spec_from_file_location("cost_report_under_test", path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _line(msg_id, *, ts="2026-08-26T10:00:00Z", model="claude-opus-5", **counts):
    usage = {
        "input_tokens": counts.get("input", 0),
        "output_tokens": counts.get("output", 0),
        "cache_creation_input_tokens": counts.get("cache_write", 0),
        "cache_read_input_tokens": counts.get("cache_read", 0),
    }
    message = {"usage": usage, "model": model}
    if msg_id is not None:
        message["id"] = msg_id
    return json.dumps({"timestamp": ts, "message": message})


def _write(tmp_path, lines, name="transcript.jsonl"):
    p = tmp_path / name
    p.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return p


def _row(minutes_ago, usd, *, anchor=dt.datetime(2026, 8, 26, 12, 0, tzinfo=UTC)):
    return tc.UsageRow(
        message_id=f"m{minutes_ago}",
        ts=anchor - dt.timedelta(minutes=minutes_ago),
        model="claude-opus-5",
        usage={},
        usd=usd,
    )


# --- dedup --------------------------------------------------------------------

def test_repeated_message_id_counted_once(tmp_path):
    """The harness writes one line per content block, each repeating the SAME
    message-level usage. Three lines, one message, one charge."""
    path = _write(tmp_path, [
        _line("msg_a", input=MTOK),
        _line("msg_a", input=MTOK),
        _line("msg_a", input=MTOK),
    ])
    rows = tc.usage_rows(path)
    assert len(rows) == 1
    assert tc.price_weighted_usd(rows) == 5.0  # not 15.0


def test_distinct_ids_all_counted(tmp_path):
    path = _write(tmp_path, [
        _line("msg_a", input=MTOK),
        _line("msg_b", input=MTOK),
    ])
    assert tc.price_weighted_usd(tc.usage_rows(path)) == 10.0


def test_id_less_rows_counted_individually(tmp_path):
    """An id-less row gets its own synthetic key: folding them into one shared
    bucket would silently drop every such message but the first."""
    path = _write(tmp_path, [
        _line(None, input=MTOK),
        _line(None, input=MTOK),
    ])
    rows = tc.usage_rows(path)
    assert len(rows) == 2
    assert len({r.message_id for r in rows}) == 2


# --- price weighting ----------------------------------------------------------

def test_token_types_are_priced_not_summed(tmp_path):
    """Against base input: cache read 0.1x, cache write 1.25x, output 5x. Equal
    COUNTS of the four types must not produce equal cost."""
    base = tc.token_cost({"input_tokens": MTOK}, "claude-opus-5")
    assert base == 5.0
    assert tc.token_cost({"cache_read_input_tokens": MTOK}, "claude-opus-5") == 0.1 * base
    assert tc.token_cost({"cache_creation_input_tokens": MTOK}, "claude-opus-5") == 1.25 * base
    assert tc.token_cost({"output_tokens": MTOK}, "claude-opus-5") == 5.0 * base


def test_model_selects_its_own_rates():
    for key in ("opus", "sonnet", "haiku", "fable"):
        expected = tc.PRICING_USD_PER_MTOK[key]["input"]
        assert tc.token_cost({"input_tokens": MTOK}, f"claude-{key}-5") == expected


def test_unknown_model_falls_back_to_opus():
    assert tc.rates_for("some-unreleased-model") is tc.PRICING_USD_PER_MTOK["opus"]
    assert tc.rates_for(None) is tc.PRICING_USD_PER_MTOK["opus"]


def test_no_pricing_key_is_a_substring_of_another():
    """rates_for matches by substring, so overlapping keys would silently
    mis-price whichever the dict happened to reach first."""
    keys = list(tc.PRICING_USD_PER_MTOK)
    for a in keys:
        for b in keys:
            if a != b:
                assert a not in b


# --- tolerance ----------------------------------------------------------------

def test_truncated_final_line_yields_partial_result(tmp_path):
    """A live session's last line can be half-written. The rows before it still
    count; nothing raises."""
    p = tmp_path / "t.jsonl"
    p.write_text(_line("msg_a", input=MTOK) + "\n" + '{"timestamp": "2026-08', encoding="utf-8")
    rows = tc.usage_rows(p)
    assert len(rows) == 1
    assert rows[0].message_id == "msg_a"


def test_missing_file_yields_nothing(tmp_path):
    assert tc.usage_rows(tmp_path / "nope.jsonl") == []
    assert list(tc.iter_jsonl(tmp_path / "nope.jsonl")) == []


def test_rows_without_usage_are_skipped(tmp_path):
    path = _write(tmp_path, [
        json.dumps({"type": "user", "message": {"role": "user", "content": "hi"}}),
        json.dumps({"type": "summary", "summary": "x"}),
        json.dumps({"message": "not-a-dict"}),
        _line("msg_a", input=MTOK),
    ])
    rows = tc.usage_rows(path)
    assert [r.message_id for r in rows] == ["msg_a"]


def test_unparseable_timestamp_does_not_raise(tmp_path):
    path = _write(tmp_path, [_line("msg_a", ts="not-a-date", input=MTOK)])
    rows = tc.usage_rows(path)
    assert rows[0].ts is None
    assert rows[0].usd == 5.0


def test_naive_timestamp_read_as_utc():
    assert tc.parse_ts("2026-08-26T10:00:00") == dt.datetime(2026, 8, 26, 10, 0, tzinfo=UTC)
    assert tc.parse_ts("2026-08-26T10:00:00Z") == dt.datetime(2026, 8, 26, 10, 0, tzinfo=UTC)
    assert tc.parse_ts(None) is None
    assert tc.parse_ts("") is None


# --- window_rate --------------------------------------------------------------

def test_window_rate_divides_by_observed_span():
    """Denominator is the span the transcript actually covers inside the window,
    not the window's nominal length — a young session must not be diluted by time
    that never happened."""
    rows = [_row(60, 4.0), _row(0, 2.0)]
    assert tc.window_rate(rows, 3.0) == 6.0  # $6 over 1 observed hour, not over 3


def test_window_rate_excludes_rows_outside_the_window():
    rows = [_row(600, 100.0), _row(60, 4.0), _row(0, 2.0)]
    assert tc.window_rate(rows, 3.0) == 6.0


def test_window_rate_abstains_below_min_span():
    """A two-minute-old session's first expensive turn must not read as an
    enormous per-hour rate."""
    rows = [_row(2, 1.0), _row(0, 1.0)]
    assert tc.window_rate(rows, 3.0, min_span_h=1.0) is None
    assert tc.window_rate(rows, 3.0) is not None


def test_window_rate_abstains_on_too_few_rows():
    assert tc.window_rate([_row(0, 5.0)], 3.0) is None
    assert tc.window_rate([], 3.0) is None
    assert tc.window_rate([tc.UsageRow("m", None, None, {}, 5.0)], 3.0) is None


def test_window_rate_honours_explicit_anchor():
    anchor = dt.datetime(2026, 8, 26, 12, 0, tzinfo=UTC)
    rows = [_row(60, 4.0), _row(0, 2.0)]
    # Anchoring an hour later halves the rate: same money, twice the span.
    later = anchor + dt.timedelta(hours=1)
    assert tc.window_rate(rows, 3.0, anchor=later) == 3.0


# --- single-source invariants -------------------------------------------------

def test_pricing_table_is_dated():
    assert tc.PRICING_VERIFIED
    dt.date.fromisoformat(tc.PRICING_VERIFIED)


def test_cost_report_re_exports_the_same_table_object():
    """Identity, not equality: two equal-but-separate tables would drift apart on
    the first rate correction, which is the defect this module removes."""
    cost_report = _load_cost_report()
    assert cost_report.PRICING_USD_PER_MTOK is tc.PRICING_USD_PER_MTOK
    assert cost_report.PRICING_SHA == tc.PRICING_SHA
    assert cost_report.token_cost is tc.token_cost


def test_pricing_sha_tracks_the_table_content():
    import hashlib

    expected = hashlib.sha256(
        json.dumps(tc.PRICING_USD_PER_MTOK, sort_keys=True).encode("utf-8")
    ).hexdigest()[:12]
    assert tc.PRICING_SHA == expected
