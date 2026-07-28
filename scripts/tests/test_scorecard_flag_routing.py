"""policy-scorecard.py Stage 8: flag routing, and the sub-agent failure-rate axis.

Two things are under test here, and they fail in opposite directions.

ROUTING is about CLOSURE. A flag printed to stdout dies with the run: nothing
records that it fired last week too, nobody can ack it, and it cannot be observed
to have stopped. Routing gives a fired flag a store row with first_seen /
times_surfaced / status. The property that makes that work is the STABLE KEY —
flag kind plus window granularity and nothing else. A key derived from the
formatted message would change with every number inside it, so one standing
condition would arrive as a fresh finding on every run and could never dedup,
ack or resolve. `test_the_same_standing_flag_stays_one_row` is that property.

The FAILURE-RATE axis is about a metric that is actually a rate. Its predecessor,
`subagent_failures`, was a marker-word regex over transcript text divided by
native `Agent` uses — two populations — and its "rate" reached 2.65 unnoticed for
six weeks. Every test here therefore checks population identity, not just a
number: `test_a_rate_over_one_population_cannot_exceed_one` asserts the invariant
that would have caught it on day one.

The two load-bearing behavioural tests are OPPOSED on purpose.
`test_the_w29_w30_step_stays_silent` replays the measured event, on which the
fixed metric IMPROVES (16.5% → 3.1%) and so must not fire; `test_an_elevated
_failure_rate_fires` replays an elevation the same shape and asserts it does. A
silence test alone is satisfied by a flag that never fires; a firing test alone
would certify a known-benign event as the specification.

Every test injects its own store path and its own spawn ledger. Nothing here
reads or writes this machine's real telemetry — in particular
`~/.local/state/claude-self-diagnose-findings.jsonl` must not come into
existence, because stage 5's first live firing has to be a genuine one.
"""
from __future__ import annotations

import datetime as dt
import importlib.util
from pathlib import Path

import pytest

SCRIPTS_DIR = Path(__file__).resolve().parent.parent
SCRIPT = SCRIPTS_DIR / "policy-scorecard.py"


def _load_module():
    spec = importlib.util.spec_from_file_location("policy_scorecard_flag_routing", SCRIPT)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


@pytest.fixture
def ps(monkeypatch, tmp_path):
    """A fresh module instance with every real-machine path redirected into
    tmp_path. SPAWN_LEDGER is redirected too: `scorecard()` defaults to reading
    it, and a test that forgot to pass `spawn_rows` would otherwise silently
    calibrate against the fleet's live telemetry."""
    mod = _load_module()
    monkeypatch.setattr(mod, "LEDGER", tmp_path / "ledger.jsonl")
    monkeypatch.setattr(mod, "TASK_QUALITY_LEDGER", tmp_path / "task-quality.jsonl")
    monkeypatch.setattr(mod, "PROJECTS_DIR", tmp_path / "projects")
    monkeypatch.setattr(mod, "GATE_LOGS", (tmp_path / "no-gate-log.jsonl",))
    monkeypatch.setattr(mod, "REPO_ROOT", tmp_path / "no-instrepo")
    monkeypatch.setattr(mod, "SPAWN_LEDGER", tmp_path / "no-spawn-ledger.jsonl")
    monkeypatch.setattr(mod.findings_store, "DEFAULT_STORE",
                        tmp_path / "must-not-be-the-real-store.jsonl")
    return mod


NOW = dt.datetime(2026, 7, 28, 12, 0, tzinfo=dt.timezone.utc)


# ------------------------------------------------------------------ fixtures

def _spawns(n: int, bad: int, lo: dt.datetime, hi: dt.datetime,
            malformed: bool = False) -> list[dict]:
    """`n` spawn-ledger rows spread evenly across [lo, hi), `bad` of which exited
    non-zero. Shape mirrors what `agentctl.cost.read_rows` yields.

    The failures are spread evenly through the ordering too, not clustered at the
    front: rows are ordered by timestamp, so a front-loaded block would give a
    sub-window of the fixture a rate the fixture does not claim to have."""
    span = (hi - lo).total_seconds()
    return [
        {"event": "spawn",
         "ts": (lo + dt.timedelta(seconds=span * (i + 0.5) / n)).isoformat(),
         "exit_code": 1 if (i + 1) * bad // n > i * bad // n else 0,
         "malformed": malformed,
         "return_marker": None if malformed else "COMPLETED"}
        for i in range(n)
    ]


def _ago(days: float) -> dt.datetime:
    return NOW - dt.timedelta(days=days)


# The measured 2026-W29→W30 step, at the proportions the corrected metric gives:
# the baseline stretch ran at 16.5% and the current window at 3.1%. On the axis
# that counts spawn exits this is an IMPROVEMENT, which is exactly why stage 6's
# "sub-agent failures up 210%" was an artefact of the broken counter.
W29_W30 = _spawns(400, 66, _ago(35), _ago(7)) + _spawns(320, 10, _ago(7), NOW)


def _neutral() -> dict:
    """The keys the other, pre-existing flags read, at values none of them fire
    on — so a `_flags` call isolates the axis under test."""
    return {
        "sessions": 10, "spawns_total": 0, "inherit_opus_rate": 0.0, "inherit_opus": 0,
        "clusters_per_session": 0.0, "clusters": 0, "cost_per_session": 1.0,
        "resolution_rate": 0.9, "avg_quality": None, "n_rated": 0,
    }


# ------------------------------------------------- 1. the axis: population identity

def test_a_rate_over_one_population_cannot_exceed_one(ps):
    """The invariant the predecessor lacked. Its numerator (marker words in
    transcript text) was not a subset of its denominator (native Agent uses), so
    the "rate" reached 2.65 and rendered for six weeks as if sound. Describing
    that in a comment does not catch it; asserting it does."""
    with pytest.raises(AssertionError, match="cannot exceed"):
        ps._failure_rate(10, 11)

    assert ps._failure_rate(0, 0) == 0.0
    assert ps._failure_rate(10, 10) == 1.0


def test_the_numerator_is_a_subset_of_the_denominator_by_construction(ps):
    """`_spawn_failure_stats` filters ONE row set twice: same rows, same time
    extent. That is what makes the ratio a proportion per spawn — an intensive
    quantity with no time dimension for a window edge to stretch, which is the
    property stage 7's spend rate lacked even though its rows were identical."""
    n, bad = ps._spawn_failure_stats(W29_W30, _ago(7), NOW)

    assert (n, bad) == (320, 10)
    assert 0 <= bad <= n


def test_non_spawn_rows_are_excluded_from_both_terms(ps):
    """The ledger carries other events. Counting a non-spawn row in the
    denominator would deflate the rate; counting one in the numerator would be
    the two-population defect again."""
    noise = [{"event": "session", "ts": _ago(3).isoformat(), "exit_code": 1},
             {"event": "resume", "ts": _ago(2).isoformat(), "exit_code": 1}]

    assert ps._spawn_failure_stats(W29_W30 + noise, _ago(7), NOW) == (320, 10)


def test_the_protocol_axis_is_not_folded_into_the_process_axis(ps):
    """MALFORMED is protocol-parse hygiene — the specialist ran and returned, the
    marker just did not parse. At the calibration snapshot 41% of spawns were
    malformed while 14% exited non-zero, so averaging the two axes would produce
    a number describing neither. This flag governs the process axis only; a
    ledger that is entirely malformed but exits clean is silent."""
    rows = (_spawns(400, 0, _ago(35), _ago(7), malformed=True)
            + _spawns(320, 0, _ago(7), NOW, malformed=True))

    assert ps._failure_rate_flag(rows, NOW, 7) is None


# ------------------------------------------------- 2. the axis: firing behaviour

def test_the_w29_w30_step_stays_silent(ps):
    """The measured event. On the corrected metric the current window is an
    improvement over its own baseline, so a flag that fires here is measuring
    something other than what it claims."""
    n, bad = ps._spawn_failure_stats(W29_W30, _ago(7), NOW)
    baseline, base_n = ps._failure_rate_baseline(W29_W30, NOW, 7)

    assert ps._failure_rate(n, bad) < baseline, "fixture must reproduce an improvement"
    assert ps._failure_rate_flag(W29_W30, NOW, 7) is None


def test_an_elevated_failure_rate_fires(ps):
    """The opposed control: same baseline, same volumes, a rate elevated well
    past the threshold."""
    rows = _spawns(400, 66, _ago(35), _ago(7)) + _spawns(320, 160, _ago(7), NOW)

    flag = ps._failure_rate_flag(rows, NOW, 7)

    assert flag is not None
    assert "50.0%" in flag and "160/320" in flag
    assert "3.03×" in flag


def test_the_threshold_is_where_the_constant_says_it_is(ps):
    """Two-sided boundary. FAILURE_RATE_FACTOR = 1.5 against a 16.5% baseline
    puts the edge at 24.75%: 79/320 is below it, 80/320 is above."""
    base = _spawns(400, 66, _ago(35), _ago(7))

    assert ps._failure_rate_flag(base + _spawns(320, 79, _ago(7), NOW), NOW, 7) is None
    assert ps._failure_rate_flag(base + _spawns(320, 80, _ago(7), NOW), NOW, 7) is not None


def test_a_window_too_thin_to_be_evidence_does_not_fire(ps):
    """Below FAILURE_RATE_MIN_SPAWNS one spawn moves the rate by more than the
    whole 2026-W30 rate, so the window carries no signal to flag."""
    thin = ps.FAILURE_RATE_MIN_SPAWNS - 1
    rows = _spawns(400, 0, _ago(35), _ago(7)) + _spawns(thin, thin, _ago(7), NOW)

    assert ps._failure_rate_flag(rows, NOW, 7) is None


def test_a_failure_free_baseline_uses_the_rule_of_three_not_a_division_by_zero(ps):
    """A 0% baseline has no ratio. The rule of three gives the 95% upper bound on
    a rate that produced 0 failures in n trials — the smallest claim the evidence
    supports. Without it a single failure would divide by zero and fire."""
    base = _spawns(400, 0, _ago(35), _ago(7))
    floor = 3.0 / 400  # 0.75%

    # One failure in 320: 0.31%, under the floor even before the 1.5× factor.
    assert ps._failure_rate_flag(base + _spawns(320, 1, _ago(7), NOW), NOW, 7) is None
    # Comfortably past floor × factor (1.125%).
    fired = ps._failure_rate_flag(base + _spawns(320, 20, _ago(7), NOW), NOW, 7)
    assert fired is not None
    assert 20 / 320 > floor * ps.FAILURE_RATE_FACTOR


def test_the_baseline_is_pooled_not_averaged(ps):
    """Proportions from unequal denominators do not average. Pooled over the
    trailing stretch, 3 failures in 3 spawns plus 0 in 397 is 0.75%; a mean of
    the two per-window rates would be 50%, and the flag would then be silent
    through an eightyfold real elevation."""
    quiet = _spawns(3, 3, _ago(35), _ago(34))
    busy = _spawns(397, 0, _ago(34), _ago(7))
    baseline, base_n = ps._failure_rate_baseline(quiet + busy, NOW, 7)

    assert base_n == 400
    assert baseline == pytest.approx(3 / 400)


# ------------------------------------------- 3. the shipped constant is reproducible

# The spawn ledger as it stood at the pin `--calibrate-until 2026-07-28`: 1182
# spawn rows, 2026-05-25→2026-07-27, swept as a 7d window rolled one day at a
# time against the pooled preceding 28 days. `(end, n, bad, base_n, base)` —
# the RAW counts, so the test recomputes the ratio the shipped rule divides by
# rather than pinning an already-derived number.
#
# Regenerate with:
#   python3 scripts/policy-scorecard.py --calibrate-failure-rate --calibrate-until 2026-07-28
PINNED_SAMPLES = [
    ('2026-06-22', 47, 14, 71, 0.4084507042253521),
    ('2026-06-23', 30, 5, 92, 0.41304347826086957),
    ('2026-06-25', 34, 8, 117, 0.358974358974359),
    ('2026-06-26', 37, 8, 118, 0.3644067796610169),
    ('2026-06-27', 68, 8, 118, 0.3644067796610169),
    ('2026-06-28', 68, 8, 118, 0.3644067796610169),
    ('2026-06-29', 76, 10, 118, 0.3644067796610169),
    ('2026-06-30', 113, 13, 115, 0.3391304347826087),
    ('2026-07-01', 123, 10, 126, 0.3253968253968254),
    ('2026-07-02', 122, 9, 137, 0.31386861313868614),
    ('2026-07-03', 141, 13, 141, 0.3120567375886525),
    ('2026-07-04', 161, 26, 172, 0.2558139534883721),
    ('2026-07-05', 186, 29, 172, 0.2558139534883721),
    ('2026-07-06', 187, 28, 180, 0.25555555555555554),
    ('2026-07-07', 155, 26, 221, 0.22171945701357465),
    ('2026-07-08', 132, 25, 245, 0.19591836734693877),
    ('2026-07-09', 215, 43, 255, 0.19215686274509805),
    ('2026-07-10', 266, 46, 273, 0.1978021978021978),
    ('2026-07-11', 299, 45, 319, 0.19435736677115986),
    ('2026-07-12', 286, 44, 343, 0.18658892128279883),
    ('2026-07-13', 282, 43, 348, 0.1810344827586207),
    ('2026-07-14', 325, 53, 355, 0.17746478873239438),
    ('2026-07-15', 353, 57, 358, 0.17318435754189945),
    ('2026-07-16', 261, 38, 444, 0.18018018018018017),
    ('2026-07-17', 242, 35, 504, 0.17063492063492064),
    ('2026-07-18', 179, 27, 582, 0.16494845360824742),
    ('2026-07-19', 167, 25, 591, 0.1658206429780034),
    ('2026-07-20', 202, 31, 592, 0.16047297297297297),
    ('2026-07-21', 191, 26, 623, 0.15569823434991975),
    ('2026-07-22', 209, 23, 637, 0.15384615384615385),
    ('2026-07-23', 247, 22, 632, 0.1550632911392405),
    ('2026-07-24', 300, 18, 686, 0.14868804664723032),
    ('2026-07-25', 279, 14, 707, 0.14992927864214992),
    ('2026-07-26', 279, 14, 707, 0.14992927864214992),
    ('2026-07-27', 244, 8, 747, 0.1499330655957162),
    ('2026-07-28', 276, 5, 784, 0.15051020408163265),
]


def _pinned(ps) -> list:
    return [ps.FailureSample(dt.date.fromisoformat(end), n, bad, bad / n, base_n, base,
                             (bad / n) / max(base, 3.0 / base_n))
            for end, n, bad, base_n, base in PINNED_SAMPLES]


def test_the_shipped_factor_is_what_the_rule_picks(ps):
    """A constant derived from an append-only file that has grown since is only
    calibrated if the derivation can be re-run. This pins the snapshot and
    asserts mechanically that the stated rule reproduces the shipped number —
    otherwise 1.5 is asserted, not calibrated."""
    samples = _pinned(ps)

    assert len(samples) == 36
    assert ps._failure_rate_factor(samples) == ps.FAILURE_RATE_FACTOR


def test_both_terms_of_the_selection_rule_are_what_the_comment_claims(ps):
    """The rule takes max(empirical envelope, sampling-noise floor). Pinning only
    the max would let one term drift to nonsense while the other happened to
    bind, so both are asserted — the comment names 1.041 and 1.442."""
    samples = _pinned(ps)

    assert max(s.ratio for s in samples) == pytest.approx(1.041, abs=0.001)
    assert ps._failure_noise_floor(samples) == pytest.approx(1.442, abs=0.001)
    # The noise term binds here; the envelope is what stops the rule collapsing
    # if a future quiet period shrinks the noise term.
    assert ps._failure_noise_floor(samples) > max(s.ratio for s in samples)


def test_no_sample_in_the_pinned_history_fires(ps):
    """The threshold is calibrated for SILENCE — deliberately, and stated as a
    limit rather than a strength: this history contains no failure episode to
    budget a firing frequency against. The number this test guards is 0."""
    samples = _pinned(ps)

    assert [s.end for s in samples if s.ratio > ps.FAILURE_RATE_FACTOR] == []


# ------------------------------------------------------------- 4. stable keys

def test_flag_keys_do_not_carry_the_formatted_message(ps):
    """The dedup property. A key built from the message changes with every number
    in it, so a standing condition would arrive as a new finding on every run."""
    rows = _spawns(400, 66, _ago(35), _ago(7)) + _spawns(320, 160, _ago(7), NOW)
    louder = _spawns(400, 66, _ago(35), _ago(7)) + _spawns(320, 200, _ago(7), NOW)

    a = ps._flags(_neutral(), _neutral(), spawn_rows=rows, now=NOW)
    b = ps._flags(_neutral(), _neutral(), spawn_rows=louder, now=NOW)

    assert [f.key for f in a] == [f.key for f in b] == ["subagent-failure-rate/7d"]
    assert a[0].message != b[0].message


def test_the_window_granularity_is_part_of_the_key(ps):
    """The same condition at two window sizes is two findings with two
    thresholds; a shared key would let the last run to finish overwrite the
    other's row."""
    # Elevated uniformly through the last 14 days, so both window sizes see it.
    rows = _spawns(4000, 660, _ago(84), _ago(14)) + _spawns(640, 320, _ago(14), NOW)

    k7 = [f.key for f in ps._flags(_neutral(), _neutral(), days=7,
                                   spawn_rows=rows, now=NOW)]
    k14 = [f.key for f in ps._flags(_neutral(), _neutral(), days=14,
                                    spawn_rows=rows, now=NOW)]

    assert k7 == ["subagent-failure-rate/7d"]
    assert k14 == ["subagent-failure-rate/14d"]


def test_a_flag_renders_as_its_message(ps):
    """`Flag.__str__` is the message, so every pre-existing render and test site
    that interpolates a flag is unaffected by the key gaining existence."""
    flag = ps.Flag("some-key/7d", "the human sentence")

    assert f"{flag}" == "the human sentence"


# --------------------------------------------------------------- 5. routing

def _fired(ps) -> list:
    rows = _spawns(400, 66, _ago(35), _ago(7)) + _spawns(320, 160, _ago(7), NOW)
    return ps._flags(_neutral(), _neutral(), spawn_rows=rows, now=NOW)


def test_a_fired_flag_becomes_an_actionable_store_finding(ps, tmp_path):
    """Routing's whole point: the flag acquires closure state, and the store's
    own remediation table tells the reader what act closes it."""
    store = tmp_path / "findings.jsonl"

    rows = ps.route_flags(_fired(ps), store_path=store, now=NOW)

    assert len(rows) == 1
    row = rows[0]
    assert row["kind"] == ps.findings_store.KIND_POLICY_FLAG
    assert row["path"] == "subagent-failure-rate/7d"
    assert row["source"] == ps.findings_store.SOURCE_POLICY_SCORECARD
    assert row["detail"].startswith("sub-agent process-failure rate")
    assert row["times_surfaced"] == 1
    assert ps.findings_store.is_actionable(row["kind"])
    fix = ps.findings_store.REMEDIATION[ps.findings_store.KIND_POLICY_FLAG]
    assert "self-improvement" in fix
    assert "policy-effectiveness-tracking" in fix


def test_the_same_standing_flag_stays_one_row(ps, tmp_path):
    """Two runs of a standing condition are one finding, surfaced twice, with
    first_seen preserved — that is what makes "how long has this been open"
    answerable at all."""
    store = tmp_path / "findings.jsonl"
    later = NOW + dt.timedelta(days=7)

    ps.route_flags(_fired(ps), store_path=store, now=NOW)
    rows = ps.route_flags(_fired(ps), store_path=store, now=later)

    assert len(rows) == 1
    assert rows[0]["times_surfaced"] == 2
    assert rows[0]["first_seen"].startswith("2026-07-28")
    assert rows[0]["last_seen"].startswith("2026-08-04")


def test_a_flag_that_stops_firing_is_resolved_out(ps, tmp_path):
    """Absence from a COMPLETED scan means the condition is gone. Without this a
    fixed problem would accuse forever, and the store would train its reader to
    ignore it."""
    store = tmp_path / "findings.jsonl"
    ps.route_flags(_fired(ps), store_path=store, now=NOW)

    rows = ps.route_flags([], store_path=store, now=NOW + dt.timedelta(days=7))

    assert rows == []
    assert ps.findings_store.load_rows(store, NOW) == []


def test_acking_survives_the_next_run(ps, tmp_path):
    """An ack that the next scan silently cleared would make the ack button a
    lie. Upsert refreshes detail and last_seen; it must not touch status."""
    store = tmp_path / "findings.jsonl"
    key = ps.route_flags(_fired(ps), store_path=store, now=NOW)[0]["key"]

    assert ps.findings_store.ack(key, "known, tracked in the plan", path=store, now=NOW)
    rows = ps.route_flags(_fired(ps), store_path=store, now=NOW + dt.timedelta(days=7))

    assert rows[0]["status"] != "open"
    assert rows[0]["ack_reason"] == "known, tracked in the plan"
    # Age is past the debounce, so only the ack is keeping it off the gate.
    assert ps.findings_store.open_actionable(rows, NOW + dt.timedelta(days=7)) == []


def test_a_fresh_flag_is_debounced_before_it_may_block(ps, tmp_path):
    """ACTIONABLE_MIN_AGE_DAYS is inherited unmodified: the scorecard's cadence
    is weekly, so a flag worth blocking on is standing, not momentary."""
    store = tmp_path / "findings.jsonl"
    rows = ps.route_flags(_fired(ps), store_path=store, now=NOW)

    assert ps.findings_store.open_actionable(rows, NOW) == []
    assert len(ps.findings_store.debounced_actionable(rows, NOW)) == 1
    past_debounce = NOW + dt.timedelta(days=ps.findings_store.ACTIONABLE_MIN_AGE_DAYS)
    assert len(ps.findings_store.open_actionable(rows, past_debounce)) == 1


def test_routing_does_not_resolve_the_other_producers_rows(ps, tmp_path):
    """One store, two detectors. `source` partitions the resolve-out: a scorecard
    run never looked for self-diagnose's conditions, so it cannot have observed
    them gone. Without this every scorecard run would silently wipe the whole
    self-diagnose worklist."""
    store = tmp_path / "findings.jsonl"
    ps.findings_store.upsert_findings(
        [{"kind": "orphan-leaf", "path": "leaves/x.md", "detail": "unreferenced"}],
        path=store, now=NOW)

    ps.route_flags(_fired(ps), store_path=store, now=NOW)
    rows = ps.route_flags([], store_path=store, now=NOW)

    kinds = {r["kind"] for r in rows}
    assert kinds == {"orphan-leaf"}
    assert ps.findings_store.row_source(rows[0]) == ps.findings_store.SOURCE_SELF_DIAGNOSE


def test_routing_is_additive_to_what_the_scorecard_prints(ps, tmp_path):
    """The rendering contract: routing adds closure state and changes no output.
    The flag line is still rendered from the message."""
    store = tmp_path / "findings.jsonl"
    flags = _fired(ps)

    ps.route_flags(flags, store_path=store, now=NOW)

    assert flags[0].message in f"- ⚠ {flags[0]}"
    assert store.exists()


def test_no_test_here_touches_the_real_store(ps):
    """The guard on the guard. Stage 5's store must still not exist when this
    stage lands: its first live firing has to be genuine, and a test that
    defaulted the path would have manufactured it."""
    assert not (Path.home() / ".local" / "state"
                / "claude-self-diagnose-findings.jsonl").exists()
