"""`agentctl effort-check`: report the four scales, write NOTHING.

The effort trigger only ever compares inside a command the coordinator chose to
run. This command is the observation that does not wait for such a call — and the
whole reason it can be run from a UserPromptSubmit hook on every prompt is that it
is inert. If it wrote, it would be a SECOND fire site: it would consume the
one-fire-per-replan budget belt 2 keeps (`effort._replans_since_last_fire`) with
nobody having diagnosed anything, and the real fire site would then fall silent.

So the load-bearing assertion here is the negative one: the state file's bytes are
unchanged across the call.
"""
import json
from argparse import Namespace

from agentctl import cli, effort, task_accumulator
from agentctl.config import Thresholds
from agentctl.state import Node
from conftest import STAGE_OBSERVATIONS


def ns(**kw):
    base = dict(cost_log=None)
    base.update(kw)
    return Namespace(**base)


def _to_approved(store, fixtures_dir, sid, task="demo"):
    cli.cmd_start(ns(session=sid, task=task, goal="g", done_criterion="dc",
                     criterion_type="measurable", recursion_depth=0), store=store)
    cli.cmd_classify(ns(session=sid, chat=False, changed_lines=200, files=5,
                        wall_clock_min=60, tracker_key=None, architectural=True,
                        external_effect=False, new_dependency=False,
                        public_api_change=False), store=store)
    cli.cmd_plan(ns(session=sid), store=store)
    cli.cmd_submit_plan(ns(session=sid, plan=str(fixtures_dir / "plan_two_stage.toml")),
                        store=store)
    cli.cmd_approve(ns(session=sid, by="user"), store=store)


def test_no_session_is_ok_and_silent(store):
    d = cli.cmd_effort_check(ns(session="nope"), store=store)
    assert d.ok is True
    assert d.data["armed"] is False
    assert d.data["scales"] == []


def test_unarmed_session_reports_unarmed(store):
    """A session with no approval baseline: no scale CAN diverge, and saying so is
    not an error — the hook driving this must be able to tell "nothing to measure"
    from "measured, nothing wrong"."""
    sid = "ec-unarmed"
    cli.cmd_start(ns(session=sid, task="demo", goal="g", done_criterion="dc",
                     criterion_type="measurable", recursion_depth=0), store=store)
    d = cli.cmd_effort_check(ns(session=sid), store=store)
    assert d.ok is True
    assert d.data["armed"] is False
    assert d.data["scales"] == []


def test_reports_every_scale_with_its_comparand(store, fixtures_dir):
    sid = "ec1"
    _to_approved(store, fixtures_dir, sid)
    d = cli.cmd_effort_check(ns(session=sid), store=store)
    assert d.ok is True
    assert d.data["armed"] is True
    assert [s["scale"] for s in d.data["scales"]] == list(effort.SCALE_ORDER)
    for row in d.data["scales"]:
        assert set(row) >= {
            "scale", "label", "unit", "kind", "actual", "comparand", "ratio",
            "past_own_trigger", "at_or_past_threshold",
        }
    # a freshly-approved session has spent nothing since its baseline
    assert d.data["over_threshold"] == []
    assert d.data["would_fire"] is None
    assert "no scale at or past its threshold" in d.detail


def test_reports_a_scale_past_its_threshold(store, fixtures_dir, monkeypatch):
    """Drive the REPLANS scale past its absolute trigger by hand (the accounting
    itself is effort.py's own tests' subject) and check the report names it."""
    sid = "ec2"
    _to_approved(store, fixtures_dir, sid)
    state = store.load(sid)
    for _ in range(Thresholds().effort_replan_absolute()):
        state.log("replan")
    store.save(state)

    d = cli.cmd_effort_check(ns(session=sid), store=store)
    assert d.ok is True                                   # a report, never a refusal
    assert effort.SCALE_REPLANS in d.data["over_threshold"]
    assert d.data["would_fire"] == effort.SCALE_REPLANS
    assert d.data["framing"]
    row = next(s for s in d.data["scales"] if s["scale"] == effort.SCALE_REPLANS)
    assert row["at_or_past_threshold"] is True
    assert row["past_own_trigger"] >= 1.0


def test_the_report_agrees_with_would_fire_across_sessions(store, fixtures_dir):
    """The load-bearing agreement: a scale the gate WOULD fire on must be a scale the
    report says is at or past its threshold.

    This is the resolved-reentry shape, and it is not hypothetical — it is what the
    other half of this same change manufactures by construction. `reset` builds a fresh
    SessionState whose own replan count is 0, while the cross-session accumulator still
    holds the prior laps. `divergence()` reads the accumulator; a report computed from
    session-local `deltas()` would therefore say "nothing over threshold" on exactly the
    session the trigger exists to catch, and the watch hook — which speaks on the report,
    not on `would_fire` — would stay silent through it.
    """
    sid = "ec-cross"
    _to_approved(store, fixtures_dir, sid, task="cross-demo")
    state = store.load(sid)
    assert effort.replan_count(state) == 0          # this session has logged none
    task_accumulator.add("cross-demo", "replan_count",
                         Thresholds().effort_replan_absolute(), session_id=sid, now=None)

    d = cli.cmd_effort_check(ns(session=sid), store=store)
    row = next(s for s in d.data["scales"] if s["scale"] == effort.SCALE_REPLANS)
    assert d.data["would_fire"] == effort.SCALE_REPLANS
    assert row["at_or_past_threshold"] is True
    assert effort.SCALE_REPLANS in d.data["over_threshold"]
    assert row["actual"] == float(Thresholds().effort_replan_absolute())
    assert row["cross_session"] is True             # and the row says where it came from
    assert "effort divergence on" in d.detail


def test_every_scale_row_agrees_with_would_fire(store, fixtures_dir):
    """`would_fire`, when set, is always one of `over_threshold` -- checked here on the
    replans row specifically (the one this change made cross-session), NOT as a general
    claim about every scale: `would_fire` can legitimately be None with `over_threshold`
    non-empty (belt 2 already spent this scale's one-fire-per-replan budget, see
    `hook-effort-divergence-watch.py`'s `already_fired` branch), and that is not a
    disagreement this test is meant to catch. A future scale that grows a cross-session
    (or otherwise non-session-local) source should extend this same replans-shaped check
    for its own row, not lean on this one to have generalized for it."""
    sid = "ec-agree"
    _to_approved(store, fixtures_dir, sid, task="agree-demo")
    task_accumulator.add("agree-demo", "replan_count", 99, session_id=sid, now=None)
    d = cli.cmd_effort_check(ns(session=sid), store=store)
    assert d.data["would_fire"] == effort.SCALE_REPLANS
    assert d.data["would_fire"] in d.data["over_threshold"]


def test_writes_nothing(store, fixtures_dir, tmp_path):
    """The load-bearing property: byte-identical state file, and no fire recorded.

    The cost row is deliberately large, so `effort.refresh_spend` has something to
    accumulate and the test exercises the MUTATING read rather than a no-op one.
    That mutation is real — it is how the spend accumulator is read at all — and
    with no `store.save` behind it, none of it reaches disk.
    """
    sid = "ec3"
    _to_approved(store, fixtures_dir, sid)
    cost_log = tmp_path / "cost.jsonl"
    cost_log.write_text(
        json.dumps({"plan_path": store.load(sid).plan_path, "cost_usd": 99.0}) + "\n",
        encoding="utf-8")

    files = sorted(p for p in (tmp_path / "state").rglob("*") if p.is_file())
    assert files, "expected the store to have written a state file for this session"
    before = {p: p.read_bytes() for p in files}

    d = cli.cmd_effort_check(ns(session=sid, cost_log=str(cost_log)), store=store)
    assert d.ok is True

    assert {p: p.read_bytes() for p in files} == before
    after = store.load(sid)
    assert not after.effort_actuals.get(effort.ACTUAL_SPEND_KEY)
    assert after.effort_spend_seen == {}
    assert after.effort_fires == []
    assert after.node == Node.APPROVED.value


def test_registered_in_the_parser():
    parser = cli.build_parser()
    args = parser.parse_args(["effort-check", "--session", "s1"])
    assert cli.COMMANDS[args.command] is cli.cmd_effort_check
