"""judge-usage-report.py — the counting side of the judge execution ledger.

What these tests pin is not "the report prints something" but the three ways
it could lie quietly:

  * under-reporting the fail-open surface, by letting the printed count drift
    away from the declared taxonomy;
  * flattering the latency table, by admitting durations that belong to calls
    which produced no judgement (or by counting one call twice);
  * mis-binding a line to the wrong invitation, which is what happens the
    moment anything reads the ledger by line order instead of invocation_id —
    two hooks running concurrently interleave their lines in the file.
"""
import importlib.util
import json
import sys
import tempfile
from pathlib import Path

SCRIPTS = Path(__file__).resolve().parents[1]

_SPEC = importlib.util.spec_from_file_location(
    "judge_usage_report", SCRIPTS / "judge-usage-report.py"
)
mod = importlib.util.module_from_spec(_SPEC)
sys.modules["judge_usage_report"] = mod  # dataclass string-annotation resolution needs this
_SPEC.loader.exec_module(mod)

from lib import judge_latency  # noqa: E402

SESSION = "sess-under-test"


def _record(kind, invocation_id, *, hook="turn_end", ts=1000.0, **fields):
    record = {
        "ts": ts,
        "kind": kind,
        "invocation_id": invocation_id,
        "hook": hook,
        "source": SESSION,
    }
    record.update(fields)
    return record


def _decided(invocation_id, *, judge="feedback_signal", stage="call", **overrides):
    """A `decided` line with every field lib/judge_ledger.decided() writes, so
    a fixture cannot accidentally pass by omitting the field a branch reads."""
    fields = {
        "judge": judge,
        "stage": stage,
        "verdict": False,
        "reason": "",
        "timed_out": False,
        "malformed": False,
        "runner_legacy": False,
        "remaining": None,
        "threshold": None,
        "ceiling": None,
        "duration": None,
    }
    hook = overrides.pop("hook", "turn_end")
    fields.update(overrides)
    return _record("decided", invocation_id, hook=hook, **fields)


def _write_ledger(tmp_path, records) -> Path:
    path = tmp_path / "judge-usage-ledger.jsonl"
    path.write_text(
        "".join(json.dumps(r, sort_keys=True) + "\n" for r in records), encoding="utf-8"
    )
    return path


def _tally(tmp_path, records):
    path = _write_ledger(tmp_path, records)
    from lib import judge_ledger

    return mod.tally(judge_ledger.read_ledger(path), path)


# One minimal fixture per OBSERVABLE outcome. Outcome 1 is deliberately absent:
# a hook that never entered writes no line, so no fixture can produce it — that
# is what the dispatch witness is for, not this report.
OUTCOME_FIXTURES = {
    "2": [_record("entered", "i2", judge="binary_ask", prefilter_fired=False)],
    "3": [_decided("i3", stage="budget", reason="budget exhausted before call (fail-open)")],
    "4": [_decided("i4", reason="", duration=3.0)],
    "5": [_decided("i5", timed_out=True, reason="judge timed out (fail-open)", duration=27.0)],
    "6": [_record("started", "i6", judge="feedback_signal")],
    "7": [_decided("i7", reason="judge exited non-zero (fail-open)", duration=0.05)],
    "7a": [_decided("i7a", malformed=True, reason="judge returned no output (fail-open)")],
    "7b": [_decided("i7b", timed_out=None, reason="judge raised (fail-open)", duration=0.04)],
    "7c": [_decided("i7c", stage="killswitch", reason="judge disabled (fail-open)")],
    "8": [_record("hook_start", "i8", hook="turn_end"), _record("discarded", "i8", reason="boom")],
    "9": [
        _record("hook_start", "i9"),
        _record("emitted", "i9", ok=False, had_directive=True),
    ],
    "10": [_record("hook_start", "i10"), _record("final", "i10", has_directive=True)],
    "11": [_record("hook_start", "i11")],
}


def test_declared_taxonomy_and_fail_open_subset_agree():
    """The two independent statements of "which outcomes fail open" — the
    per-row flag and NOT_FAIL_OPEN_IDS — must not drift apart, and the printed
    fail-open count must be their difference rather than a typed-in number."""
    all_ids = {o.id for o in mod.OUTCOMES}
    assert len(all_ids) == len(mod.OUTCOMES), "duplicate outcome id"
    assert mod.NOT_FAIL_OPEN_IDS <= all_ids
    # Exactly three outcomes leave the gate having done its job: never entered,
    # prefiltered, judged. Everything else allowed a turn through unjudged.
    assert len(mod.NOT_FAIL_OPEN_IDS) == 3
    assert len(mod.FAIL_OPEN_OUTCOMES) == len(mod.OUTCOMES) - len(mod.NOT_FAIL_OPEN_IDS)
    assert {o.id for o in mod.OUTCOMES if o.fail_open} == all_ids - mod.NOT_FAIL_OPEN_IDS


def test_the_printed_fail_open_headline_moves_with_the_taxonomy(monkeypatch, tmp_path):
    """Earlier drafts of this taxonomy had two different fail-open counts spelt
    out in the prose, and both outlived the taxonomy that justified them —
    which is exactly how a report ends up under-reporting while looking
    authoritative. Scanning the source for those two dead words only ever
    caught the words; what has to hold is that the printed sentence is DERIVED,
    so grow the taxonomy by one row and watch the headline follow."""
    result = _tally(tmp_path, [])
    before = "\n".join(mod.format_fail_open(result))
    assert f"{len(mod.FAIL_OPEN_OUTCOMES)} of the {len(mod.OUTCOMES)}" in before

    extra = mod.Outcome("12", "an outcome nobody has met yet", mod.LEVEL_JUDGE, True)
    monkeypatch.setattr(mod, "OUTCOMES", mod.OUTCOMES + (extra,))
    monkeypatch.setattr(mod, "FAIL_OPEN_OUTCOMES", mod.FAIL_OPEN_OUTCOMES + (extra,))
    after = "\n".join(mod.format_fail_open(result))
    assert f"{len(mod.FAIL_OPEN_OUTCOMES)} of the {len(mod.OUTCOMES)}" in after
    assert after != before


def test_every_observable_outcome_is_reachable_and_no_other():
    """Drives one fixture per declared observable outcome. A fifteenth outcome
    added to OUTCOMES with no fixture — or a classifier branch that stops
    producing an id — breaks this loudly instead of quietly reporting a zero."""
    observable = {o.id for o in mod.OUTCOMES if o.observable}
    assert set(OUTCOME_FIXTURES) == observable

    for outcome_id, records in OUTCOME_FIXTURES.items():
        with tempfile.TemporaryDirectory() as tmp:
            result = _tally(Path(tmp), records)
            counts = result.counts_by_outcome()
            produced = {oid for oid, count in counts.items() if count}
            assert produced == {outcome_id}, f"fixture for {outcome_id} produced {produced}"
            assert not result.complaints


def test_a_judge_that_only_ever_fails_fast_is_not_reported_as_healthy():
    """The reverse side of the report: every call returns in a tenth of a
    second with a non-zero exit — a judge binary that dies on startup. That
    shape must never read as working, and the honest-verdict row must read 0."""
    records = []
    for index in range(6):
        invocation = f"fast{index}"
        records.append(_record("hook_start", invocation))
        records.append(
            _decided(
                invocation,
                reason="judge exited non-zero (fail-open)",
                duration=0.1,
            )
        )
        records.append(_record("final", invocation, has_directive=False))
        records.append(_record("emitted", invocation, ok=True, had_directive=False))
    with tempfile.TemporaryDirectory() as tmp:
        result = _tally(Path(tmp), records)
    text = "\n".join(mod.format_report(result))
    assert "healthy" not in text.lower()
    assert "NOT RUNNING" in text
    assert result.counts_by_outcome()["4"] == 0
    assert result.counts_by_outcome()["7"] == 6


def test_duration_population_is_pinned_to_the_completed_and_timed_out_calls(tmp_path):
    """Outcomes 4 and 5 carry a real measured call. The fast-failure outcomes
    carry a duration an order of magnitude smaller, and admitting them makes a
    deader judge look faster — so the statistics must be identical to those of
    a ledger containing only the 4/5 lines."""
    population = [1.0, 2.0, 9.0, 30.0]
    records = [
        _decided("d1", reason="", duration=1.0),
        _decided("d2", reason="", duration=2.0),
        _decided("d3", reason="", duration=9.0),
        _decided("d4", timed_out=True, reason="judge timed out (fail-open)", duration=30.0),
        # Eight other outcomes, three of them carrying a duration of their own.
        _record("entered", "d5", judge="feedback_signal", prefilter_fired=False),
        _decided("d6", stage="budget", reason="budget exhausted before call (fail-open)"),
        _record("started", "d7", judge="feedback_signal"),
        _decided("d8", reason="judge exited non-zero (fail-open)", duration=0.05),
        _decided("d9", malformed=True, reason="unparseable (fail-open)", duration=0.06),
        _decided("d10", timed_out=None, reason="judge raised (fail-open)", duration=0.04),
        _decided("d11", stage="no_runner", reason="no runner (fail-open)"),
        _record("hook_start", "d12"),
        _record("discarded", "d12", reason="boom"),
    ]
    result = _tally(tmp_path, records)
    assert sorted(result.durations()) == population
    assert judge_latency.median(result.durations()) == judge_latency.median(population)
    assert judge_latency.p90(result.durations()) == judge_latency.p90(population)
    assert max(result.durations()) == max(population)

    # And the exclusion is load-bearing: the same numbers computed over every
    # duration in the ledger disagree, so a widened population would be visible
    # as a different median rather than as a rounding difference.
    everything = population + [0.05, 0.06, 0.04]
    assert judge_latency.median(everything) != judge_latency.median(population)


def test_a_verdict_rendered_and_never_emitted_is_not_reported_as_delivered(tmp_path):
    """The shape the whole ledger exists to catch, driven end to end: the hook
    judged, and the harness killed it before the answer left the process. Every
    line of it looks healthy in isolation — an honest verdict, a completed
    call — so the report is the only place the loss can show, and it must show
    in the VERDICT, not merely in a bucket count.

    Gating outcome 10 on a `final` line filed this shape as "completed and
    delivered" and printed HEALTHY over it: `final` is written after decide()
    returns, and this process never got there."""
    records = []
    for index in range(3):
        invocation = f"killed{index}"
        records.extend([
            _record("hook_start", invocation),
            _record("entered", invocation, judge="feedback_signal", prefilter_fired=True),
            _record("started", invocation, judge="feedback_signal"),
            _record("call", invocation, judge="feedback_signal", timed_out=False,
                    duration=6.0, returncode=0, raised=None),
            _decided(invocation, reason="", duration=6.0),
        ])
    result = _tally(tmp_path, records)
    text = "\n".join(mod.format_report(result))

    assert result.counts_by_outcome()["10"] == 3
    assert not result.completed
    assert mod.COMPLETED_LABEL not in text
    assert "Verdict: DEGRADED" in text
    assert "HEALTHY" not in text
    # The judge level is untouched by the invocation-level loss, and the two
    # are reported apart rather than as one total. The 3 on the right of the
    # invocation pair is the population of INVOCATIONS, which here happens to
    # coincide with the count of declared outcomes because all three failed
    # open; test_the_two_ways_to_count_an_invocation_population_agree drives a
    # ledger where the two differ.
    assert result.level_totals(mod.LEVEL_JUDGE) == (3, 0)
    assert result.invocation_count == 3
    assert result.level_totals(mod.LEVEL_INVOCATION) == (3, 3)


def test_only_a_successful_emission_is_reported_as_delivered(tmp_path):
    """One invocation per rung of the invocation-level ladder, judged by what
    the report PRINTS about each. Only the last of them may wear the word
    "delivered"; the two that are not declared outcomes must say what they are
    instead of borrowing the healthy label."""
    def invocation(name, *tail):
        return [_record("hook_start", name), *tail]

    shapes = {
        "8": invocation("v8", _record("discarded", "v8", reason="boom")),
        "9": invocation("v9", _record("emitted", "v9", ok=False, had_directive=True)),
        "10": invocation("v10", _decided("v10", reason="", duration=1.0)),
        "11": invocation("v11"),
    }
    for outcome_id, records in shapes.items():
        result = _tally(tmp_path, records)
        assert result.counts_by_outcome()[outcome_id] == 1, outcome_id
        assert not result.completed and not result.killed_in_call, outcome_id
        assert result.invocation_count == 1, outcome_id

    # Killed mid-call: an unpaired `started` with no verdict behind it. It is
    # NOT an invocation-level outcome — the kill is already outcome 6, counted
    # once, against the judge whose call was running.
    killed = invocation("vk", _record("started", "vk", judge="feedback_signal"))
    result = _tally(tmp_path, killed)
    assert result.counts_by_outcome()["6"] == 1
    assert sum(result.counts_by_outcome()[o.id] for o in mod.OUTCOMES
               if o.level == mod.LEVEL_INVOCATION) == 0
    assert dict(result.killed_in_call) == {"turn_end": 1}
    text = "\n".join(mod.format_report(result))
    assert mod.KILLED_IN_CALL_LABEL in text
    assert mod.COMPLETED_LABEL not in text

    # And the one shape that earns it.
    delivered = invocation(
        "vd",
        _decided("vd", reason="", duration=1.0),
        _record("final", "vd", has_directive=False),
        _record("emitted", "vd", ok=True, had_directive=False),
    )
    result = _tally(tmp_path, delivered)
    assert dict(result.completed) == {"turn_end": 1}
    assert mod.COMPLETED_LABEL in "\n".join(mod.format_report(result))


def test_an_invitation_level_outcome_does_not_double_count_its_own_calls(tmp_path):
    """One invocation judged three times and then threw its verdicts away. The
    discard is one invocation-level outcome over three judge-level ones — it
    has no duration of its own, and the calls beneath it are already counted."""
    records = [
        _record("hook_start", "multi"),
        _decided("multi", judge="feedback_signal", reason="", duration=2.0),
        _decided("multi", judge="binary_ask", timed_out=True,
                 reason="judge timed out (fail-open)", duration=30.0),
        _decided("multi", judge="outage_escalation", reason="", duration=2.0),
        _record("discarded", "multi", reason="RuntimeError()"),
    ]
    result = _tally(tmp_path, records)
    assert len(result.durations()) == 3
    counts = result.counts_by_outcome()
    assert counts["4"] == 2
    assert counts["5"] == 1
    assert counts["8"] == 1
    assert result.invocation_count == 1

    # The two granularities are reported apart, and the verdict is where that
    # has to be visible: summing them prints "2 fail-open" for a single allowed
    # turn that failed open once, counted once per level.
    assert result.level_totals(mod.LEVEL_JUDGE) == (3, 1)
    assert result.invocation_count == 1
    assert result.level_totals(mod.LEVEL_INVOCATION) == (1, 1)
    verdict = "\n".join(mod.format_verdict(result))
    assert verdict == (
        "Verdict: DEGRADED — 2 honest verdicts alongside 1 fail-open judge "
        "decision point and 1 fail-open hook invocation."
    )


def test_the_two_ways_to_count_an_invocation_population_agree(tmp_path):
    """The invocation level's denominator cannot be the sum of its declared
    rows, because every declared invocation-level row is fail-open: a healthy
    process is filed under INVOCATION_COMPLETED, which no row names. Summing
    the rows therefore made numerator and denominator the same set, and the
    report printed "N of N fail-open" over a ledger that was three-quarters
    healthy.

    Four invocations, one of each kind, so the two figures genuinely differ —
    and the population is checked against the buckets it is assembled from, so
    invocation_count stays derived rather than becoming a second number."""
    records = [
        # Healthy: judged, returned, delivered. No declared invocation outcome.
        _record("hook_start", "ok", hook="turn_end"),
        _decided("ok", reason="", duration=3.0),
        _record("final", "ok", has_directive=False),
        _record("emitted", "ok", ok=True, had_directive=False),
        # Threw its verdict away — outcome 8.
        _record("hook_start", "lost", hook="turn_end"),
        _record("discarded", "lost", reason="boom"),
        # Died before doing anything at all — outcome 11.
        _record("hook_start", "silent", hook="turn_end"),
        # Died inside the call — not a declared invocation outcome either.
        _record("hook_start", "killed", hook="turn_end"),
        _record("started", "killed", judge="feedback_signal"),
    ]
    result = _tally(tmp_path, records)

    recorded, fail_open = result.level_totals(mod.LEVEL_INVOCATION)
    assert (recorded, fail_open) == (4, 2)
    # The defect this pins: the old denominator was the declared rows alone.
    declared = sum(
        result.counts_by_outcome()[o.id]
        for o in mod.OUTCOMES
        if o.level == mod.LEVEL_INVOCATION
    )
    assert declared == 2 and declared != recorded

    # And the population is the sum of the buckets the same walk filled, so a
    # bucket added later cannot quietly fall out of the denominator.
    assert recorded == (
        len(result.invocation_outcomes)
        + sum(result.completed.values())
        + sum(result.killed_in_call.values())
        + sum(result.residual.values())
    )
    assert "hook invocations:      2 fail-open of 4 recorded" in "\n".join(
        mod.format_report(result)
    )


def test_the_invocation_rate_says_when_part_of_its_denominator_is_unreadable(tmp_path):
    """The same denominator, qualified. It includes the UNCLASSIFIED residual —
    the invocations whose shape the taxonomy could not read, which is exactly
    the set that MIGHT be fail-open and was not counted as such. Printed bare,
    "1 fail-open of 3 recorded" reads as "the other two are healthy", when one
    of them is a bucket that says nothing either way.

    The unqualified spelling is not untested: the case above has an empty
    residual and pins the line WITHOUT the clause, so the two together make the
    clause conditional on there being something to qualify."""
    records = [
        # Healthy: delivered, and said so.
        _record("hook_start", "ok"),
        _record("emitted", "ok", ok=True, had_directive=False),
        # Fail-open: threw its verdict away (outcome 8).
        _record("hook_start", "lost"),
        _record("discarded", "lost", reason="boom"),
        # Residual: an `emitted` whose `ok` is neither True nor False. The
        # delivery step was reached; whether it worked is unreadable.
        _record("hook_start", "torn"),
        _record("emitted", "torn", ok="yes", had_directive=False),
    ]
    result = _tally(tmp_path, records)
    assert sum(result.residual.values()) == 1

    line = next(l for l in mod.format_fail_open(result) if "hook invocations:" in l)
    assert "1 fail-open of 3 recorded" in line
    assert "of which 1 fell in the UNCLASSIFIED residual" in line


def test_a_stop_taken_before_the_call_is_not_a_verdict_killed_after_it(tmp_path):
    """Outcome 10 means a verdict was rendered and the process died before
    delivering it. A `decided` line whose stage is `budget` or one of the
    NO_CALL_STAGES reports the opposite — the judge was never called, so there
    was no verdict for a kill to come after. Gating outcome 10 on any `decided`
    at all filed every budget-exhausted and kill-switched invocation as a lost
    verdict, inflating the one row the operator is meant to act on."""
    for stage, reason in (
        ("budget", "budget exhausted before call (fail-open)"),
        ("killswitch", "judge disabled (fail-open)"),
        ("no_runner", "no runner (fail-open)"),
        ("no_text", "nothing to judge (fail-open)"),
    ):
        records = [
            _record("hook_start", "s", hook="escalation_ask"),
            _decided("s", stage=stage, reason=reason),
        ]
        result = _tally(tmp_path, records)
        counts = result.counts_by_outcome()
        assert counts["10"] == 0, stage
        # It is still counted — as the no-call outcome it actually is, once, at
        # the judge level — and the invocation itself is the residual 11.
        assert counts["3" if stage == "budget" else "7c"] == 1, stage
        assert counts["11"] == 1, stage
        assert not result.complaints, stage

    # The contrast case, so the gate is not simply switched off: a `decided`
    # from a real call with no `emitted` behind it is still outcome 10.
    records = [
        _record("hook_start", "c", hook="escalation_ask"),
        _decided("c", reason="", duration=2.0),
    ]
    result = _tally(tmp_path, records)
    assert result.counts_by_outcome()["10"] == 1


def test_interleaved_invocations_are_bound_by_id_not_by_line_order(tmp_path):
    """Two hooks running at once write into one file, so the lines of one
    invitation are not contiguous. Binding by position would attribute the
    timeout to the wrong hook and the honest call to the wrong judge."""
    records = [
        _record("hook_start", "A", hook="escalation_diagnosis"),
        _record("hook_start", "B", hook="turn_end"),
        _decided("B", hook="turn_end", judge="feedback_signal", timed_out=True,
                 reason="judge timed out (fail-open)", duration=30.0),
        _decided("A", hook="escalation_diagnosis", judge="outage_escalation",
                 reason="", duration=5.0),
        _record("final", "B", hook="turn_end", has_directive=False),
        _record("final", "A", hook="escalation_diagnosis", has_directive=True),
        _record("emitted", "A", hook="escalation_diagnosis", ok=True, had_directive=True),
        _record("emitted", "B", hook="turn_end", ok=True, had_directive=False),
    ]
    result = _tally(tmp_path, records)
    by_key = {(p.hook, p.judge): p for p in result.judge_points}
    assert by_key[("escalation_diagnosis", "outage_escalation")].outcome_id == "4"
    assert by_key[("escalation_diagnosis", "outage_escalation")].duration == 5.0
    assert by_key[("turn_end", "feedback_signal")].outcome_id == "5"
    assert by_key[("turn_end", "feedback_signal")].duration == 30.0
    assert result.invocation_count == 2


def test_an_unpaired_started_is_attributed_to_the_invocation_that_died(tmp_path):
    """The sharpest form of the same defect: two invocations open a call on the
    SAME judge, one dies mid-call, the other completes. Pairing `started` with
    the next `call` in the file blames the wrong hook."""
    records = [
        # The completed pair comes FIRST, so a reader that pairs `started` with
        # the next `call` in the file leaves the LIVE invocation's line dangling
        # and blames the wrong hook for the kill.
        _record("hook_start", "live", hook="turn_end"),
        _record("started", "live", hook="turn_end", judge="outage_escalation"),
        _record("call", "live", hook="turn_end", judge="outage_escalation",
                timed_out=False, duration=6.0, returncode=0, raised=None),
        _record("hook_start", "dead", hook="escalation_diagnosis"),
        _record("started", "dead", hook="escalation_diagnosis", judge="outage_escalation"),
    ]
    result = _tally(tmp_path, records)
    killed = [p for p in result.judge_points if p.outcome_id == "6"]
    assert len(killed) == 1
    assert killed[0].hook == "escalation_diagnosis"
    assert not result.complaints


def test_the_report_prints_a_zero_row_for_every_declared_outcome(tmp_path):
    """A silent judge is visible only if the taxonomy rows print at zero. An
    empty ledger must still list every outcome — otherwise "no honest verdict
    ever" renders as an absent line nobody notices."""
    result = _tally(tmp_path, [])
    text = "\n".join(mod.format_report(result))
    for outcome in mod.OUTCOMES:
        assert f"({outcome.id})" in text
        assert outcome.label in text
    assert "NO DATA" in text


def test_the_no_call_stages_are_folded_into_one_row_but_broken_out(tmp_path):
    """advisor writes three distinct no-call stages while the taxonomy names a
    single outcome for them. Folding them silently would hide "no text to
    judge" behind a row labelled as a kill switch."""
    records = [
        _decided("n1", stage="killswitch", reason="disabled (fail-open)"),
        _decided("n2", stage="no_text", reason="nothing to judge (fail-open)"),
        _decided("n3", stage="no_runner", reason="no runner (fail-open)"),
    ]
    result = _tally(tmp_path, records)
    assert result.counts_by_outcome()["7c"] == 3
    text = "\n".join(mod.format_report(result))
    for label in mod.NO_CALL_STAGES.values():
        assert label in text


def test_main_renders_the_whole_report_through_the_cli(tmp_path, capsys):
    """The other tests call tally()/format_report() directly, which leaves the
    argparse wiring and the print path — the only part an operator actually
    touches — unexercised. Drives the real entry point instead."""
    records = [
        _record("hook_start", "c1", hook="turn_end"),
        _decided("c1", reason="", duration=3.0),
        _record("final", "c1", has_directive=False),
        _record("emitted", "c1", ok=True, had_directive=False),
    ]
    path = _write_ledger(tmp_path, records)
    assert mod.main(["--ledger", str(path)]) == 0
    out = capsys.readouterr().out
    assert str(path) in out
    for outcome in mod.OUTCOMES:
        assert f"({outcome.id})" in out
    assert "turn_end / feedback_signal" in out
    assert "Verdict: HEALTHY" in out


def test_a_torn_line_is_counted_rather_than_silently_dropped(tmp_path):
    """A partial write leaves one unparseable line. Skipping it is right — one
    bad line must not hide every other — but skipping it SILENTLY makes
    `record_count` the count of survivors while the report presents it as the
    ledger. The reader has to say how much of the file it is speaking for."""
    from lib import judge_ledger

    path = tmp_path / "judge-usage-ledger.jsonl"
    good = _record("hook_start", "t1")
    path.write_text(
        json.dumps(good, sort_keys=True) + "\n" + '{"kind": "decid\n' + "[]\n",
        encoding="utf-8",
    )
    read = judge_ledger.read_ledger(path)
    assert len(read.records) == 1
    assert read.dropped_lines == 2

    result = mod.tally(read, path)
    assert result.dropped_lines == 2
    text = "\n".join(mod.format_report(result))
    assert "Malformed ledger lines skipped by the reader (2)" in text


def test_an_unreadable_ledger_does_not_print_as_an_empty_one(tmp_path):
    """"The ledger is empty" is a statement about the judges; "the ledger could
    not be read" is a statement about this reader. Printing the second as the
    first turns a permissions or I/O fault into a clean bill of health."""
    from lib import judge_ledger

    missing = tmp_path / "never-written.jsonl"
    missing_text = "\n".join(mod.format_report(mod.tally(
        judge_ledger.read_ledger(missing), missing
    )))
    assert "NO DATA" in missing_text

    unreadable = tmp_path / "locked"
    unreadable.mkdir()  # a directory: open() fails with an OSError that is not ENOENT
    read = judge_ledger.read_ledger(unreadable)
    assert read.error and not read.missing
    text = "\n".join(mod.format_report(mod.tally(read, unreadable)))
    assert "Verdict: UNKNOWN" in text
    assert "NO DATA" not in text

    empty = tmp_path / "empty.jsonl"
    empty.write_text("", encoding="utf-8")
    empty_text = "\n".join(mod.format_report(mod.tally(
        judge_ledger.read_ledger(empty), empty
    )))
    assert "Verdict: NO DATA" in empty_text
    assert "UNKNOWN" not in empty_text


def _torn_verdict(tmp_path, name, records, torn_lines):
    """The verdict for a ledger holding `records` plus `torn_lines` unparseable
    lines, written in that order."""
    from lib import judge_ledger

    path = tmp_path / name
    path.write_text(
        "".join(json.dumps(r, sort_keys=True) + "\n" for r in records)
        + "".join(torn_lines),
        encoding="utf-8",
    )
    return "\n".join(mod.format_verdict(mod.tally(judge_ledger.read_ledger(path), path)))


def test_a_torn_ledger_does_not_license_a_verdict_that_denies_something(tmp_path):
    """Three of the four verdicts assert a universal negative — nothing was
    written, nothing was judged, nothing failed open — and a line the reader
    could not parse is exactly the counter-example that would refute one. The
    "N malformed lines skipped" paragraph above the verdict qualifies the
    TABLES; it does not qualify a sentence claiming a thing never happened, and
    a reader who acts on the verdict alone was being told the file was clean.

    The wholly-torn case is the sharpest: every line unreadable printed as "the
    ledger is empty; no hook has written to it", which is a statement about the
    judges made from a file that says nothing about them."""
    torn = ['{"kind": "decid\n', '{"kind\n', "[]\n"]

    wholly = _torn_verdict(tmp_path, "wholly.jsonl", [], torn)
    assert "Verdict: UNKNOWN" in wholly
    assert "3 skipped" in wholly
    assert "NO DATA" not in wholly

    # Partially torn with no honest verdict among the survivors: "NOT RUNNING"
    # says every gate ran unjudged, which the unread lines may refute.
    partial = _torn_verdict(
        tmp_path, "partial.jsonl", [_record("hook_start", "p1")], torn[:2]
    )
    assert "Verdict: UNKNOWN" in partial
    assert "2 lines could not be read" in partial
    assert "NOT RUNNING" not in partial

    # Partially torn with honest verdicts and no fail-open outcome: "no
    # fail-open outcome" is the same shape of claim, so it is qualified too.
    healthy_records = [
        _record("hook_start", "h1"),
        _decided("h1", reason="", duration=3.0),
        _record("final", "h1", has_directive=False),
        _record("emitted", "h1", ok=True, had_directive=False),
    ]
    qualified = _torn_verdict(tmp_path, "healthy-torn.jsonl", healthy_records, torn[:1])
    assert "Verdict: HEALTHY (QUALIFIED)" in qualified
    assert "1 line could not be read" in qualified

    # …and the same ledger with nothing torn is the unqualified sentence, so
    # the caveat tracks the torn lines rather than being printed always.
    clean = _torn_verdict(tmp_path, "healthy-clean.jsonl", healthy_records, [])
    assert clean == "Verdict: HEALTHY — 1 honest verdicts, no fail-open outcome."

    # DEGRADED is left alone: its claim is existential and already bad news, so
    # an unread line could only add to it.
    degraded = _torn_verdict(
        tmp_path,
        "degraded.jsonl",
        healthy_records
        + [
            _record("hook_start", "h2"),
            _decided("h2", timed_out=True, reason="judge timed out (fail-open)",
                     duration=30.0),
        ],
        torn[:1],
    )
    assert degraded.startswith("Verdict: DEGRADED")
    assert "QUALIFIED" not in degraded


def test_an_unclassifiable_line_is_reported_rather_than_dropped(tmp_path):
    """A `decided` line whose reason was truncated away by the ledger's line
    cap cannot be told from a parsed verdict, so it must not be guessed at —
    and must not vanish either, which would silently shrink every total."""
    truncated = _decided("u1", reason="x")
    del truncated["reason"]
    result = _tally(tmp_path, [truncated, _record("call", "u2", judge="binary_ask",
                                                  timed_out=False, duration=1.0,
                                                  returncode=0, raised=None)])
    assert sum(result.counts_by_outcome().values()) == 0
    assert len(result.complaints) == 2
    text = "\n".join(mod.format_report(result))
    assert "Unclassified ledger lines (2)" in text
