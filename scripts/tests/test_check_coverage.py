"""agentctl check-coverage — read-only pre-flight for the replan coverage gate.

Lets the coordinator learn whether a corrected plan would clear
gates.replan_coverage_blockers BEFORE spending a thinker plan-review on it (a
review verdict is sha256-bound to the plan file; a coverage miss discovered
later at replan invalidates an already-paid-for review). Calls the same gate
cmd_replan uses, but never saves, never logs a gate, never transitions state."""
from argparse import Namespace

from agentctl import cli


def ns(**kw):
    return Namespace(**kw)


def _to_executing_stage1(store, sid, plan):
    cli.cmd_start(ns(session=sid, task="demo-two-stage", goal="", done_criterion="",
                     criterion_type="measurable", recursion_depth=0), store=store)
    cli.cmd_classify(ns(session=sid, chat=False, changed_lines=200, files=5,
                        wall_clock_min=60, tracker_key=None, architectural=True,
                        external_effect=False, new_dependency=False,
                        public_api_change=False), store=store)
    cli.cmd_plan(ns(session=sid), store=store)
    cli.cmd_submit_plan(ns(session=sid, plan=plan), store=store)
    cli.cmd_approve(ns(session=sid, by="user"), store=store)
    cli.cmd_partition(ns(session=sid, m1=False, m2=False, m3=False, m4=False,
                         m3_severe=False, m4_severe=False), store=store)
    cli.cmd_next_stage(ns(session=sid), store=store)


def _to_diagnosing_with_critique(store, sid, plan, *, invariants_to_preserve=None):
    """Drive a session into DIAGNOSING with a critique declaring a similarity
    neither fixture plan carries as a stage condition/invariant, guaranteeing
    the coverage gate blocks — mirrors test_replan.py's helper of the same name."""
    _to_executing_stage1(store, sid, plan)
    cli.cmd_record_result(ns(session=sid, status="failed", actual="boom"), store=store)
    cli.cmd_declare(ns(session=sid, expected="e", actual="a", mismatch="m"), store=store)
    cli.cmd_investigate(ns(session=sid, localized_expectation="le", localized_actual="la",
                           hypotheses=["h1", "h2"]), store=store)
    cli.cmd_critique(ns(session=sid, functional_ground="fg", replanning_task="rt",
                        invariants_to_preserve=invariants_to_preserve or ["keep idempotency"],
                        differences_to_remove=[], failure_address="нормативное"), store=store)
    cli.cmd_normalize(ns(session=sid, factor="reproducible cause", level="note"), store=store)


def _fixture_with_invariant(fixtures_dir, tmp_path, *, invariant_text):
    """plan_two_stage_refined.toml plus an `invariants` line on stage 1 — a
    corrected plan that DOES carry the critique's similarity forward."""
    base = (fixtures_dir / "plan_two_stage_refined.toml").read_text()
    patched = base.replace(
        'output_artifacts = ["mod.py"]',
        f'output_artifacts = ["mod.py"]\ninvariants = "{invariant_text}"',
        1,
    )
    out = tmp_path / "plan_with_invariant.toml"
    out.write_text(patched)
    return str(out)


def test_blockers_reported_and_nonzero_exit_when_invariant_dropped(store, fixtures_dir):
    sid = "cc1"
    plan = str(fixtures_dir / "plan_two_stage.toml")
    refined = str(fixtures_dir / "plan_two_stage_refined.toml")
    _to_diagnosing_with_critique(store, sid, plan)

    d = cli.cmd_check_coverage(ns(session=sid, new=refined), store=store)
    assert d.ok is False
    assert "coverage_blockers" in d.data
    assert d.data["coverage_blockers"]


def test_ok_and_zero_exit_when_invariant_carried(store, fixtures_dir, tmp_path):
    sid = "cc2"
    plan = str(fixtures_dir / "plan_two_stage.toml")
    _to_diagnosing_with_critique(store, sid, plan)
    carried = _fixture_with_invariant(fixtures_dir, tmp_path, invariant_text="keep idempotency")

    d = cli.cmd_check_coverage(ns(session=sid, new=carried), store=store)
    assert d.ok is True
    assert d.data.get("coverage_blockers") is None


def test_zero_exit_with_plain_message_when_no_critique(store, fixtures_dir):
    sid = "cc3"
    plan = str(fixtures_dir / "plan_two_stage.toml")
    refined = str(fixtures_dir / "plan_two_stage_refined.toml")
    _to_executing_stage1(store, sid, plan)  # no declare/investigate/critique -> no difficulty

    d = cli.cmd_check_coverage(ns(session=sid, new=refined), store=store)
    assert d.ok is True
    assert "nothing to cover" in d.detail
    assert not d.data


def test_check_coverage_mutates_no_session_state(store, fixtures_dir):
    """The core read-only contract: bytes and mtime of the session's state file
    must be byte-for-byte identical before and after the call, whether or not
    the check finds blockers."""
    sid = "cc4"
    plan = str(fixtures_dir / "plan_two_stage.toml")
    refined = str(fixtures_dir / "plan_two_stage_refined.toml")
    _to_diagnosing_with_critique(store, sid, plan)

    state_path = store.path(sid)
    before_bytes = state_path.read_bytes()
    before_mtime = state_path.stat().st_mtime_ns

    d = cli.cmd_check_coverage(ns(session=sid, new=refined), store=store)
    assert d.ok is False  # exercising the blockers branch, the more code-heavy path

    after_bytes = state_path.read_bytes()
    after_mtime = state_path.stat().st_mtime_ns
    assert after_bytes == before_bytes
    assert after_mtime == before_mtime
