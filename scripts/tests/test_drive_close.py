"""drive / close orchestrators: the opening and closing spine collapsed into one
call each, with the two human gates (plan-approval, resolution) never auto-crossed.

All cases drive cmd_* directly through the fake `store` fixture — no real `claude -p`
and no filesystem-of-record. The substantive fixture's stages are spawn:developer, so
recording a passed result needs a --control attestation (close threads it)."""
from argparse import Namespace

from agentctl import cli
from agentctl.state import Node


def ns(**kw):
    return Namespace(**kw)


# Full classify-signal set so cmd_classify (called by drive) sees concrete values.
def _drive_ns(session, **over):
    base = dict(
        session=session, chat=False, changed_lines=0, files=1, wall_clock_min=0,
        tracker_key=None, architectural=False, external_effect=False,
        new_dependency=False, public_api_change=False,
        plan=None, approved_by=None,
        m1=False, m2=False, m3=False, m4=False, m3_severe=False, m4_severe=False,
    )
    base.update(over)
    return Namespace(**base)


def _close_ns(session, **over):
    base = dict(session=session, status=None, actual="", control=None, confirmed_by=None)
    base.update(over)
    return Namespace(**base)


def _start(store, sid, **over):
    base = dict(session=sid, task="t", goal="g", done_criterion="dc",
                criterion_type="measurable", recursion_depth=0)
    base.update(over)
    return cli.cmd_start(Namespace(**base), store=store)


SUBSTANTIVE = dict(changed_lines=200, files=5, wall_clock_min=60, architectural=True)


# --- drive ------------------------------------------------------------------

def test_drive_to_plan_ready_stops_at_gate(store, fixtures_dir):
    """Substantive drive with no --approved-by stops at PLAN_READY (the gate-stop)."""
    plan = str(fixtures_dir / "plan_two_stage.toml")
    _start(store, "d1")
    d = cli.cmd_drive(_drive_ns("d1", plan=plan, **SUBSTANTIVE), store=store)
    assert d.node == Node.PLAN_READY.value
    assert d.marker == "PLAN-READY"
    assert d.action == "await_user_approval"
    # the trace shows it walked classify -> plan -> submit-plan, then stopped
    cmds = [c["command"] for c in d.data["trace"]]
    assert cmds == ["classify", "plan", "submit_plan"]
    assert store.load("d1").node == Node.PLAN_READY.value


def test_drive_to_executing_with_approver(store, fixtures_dir):
    """--approved-by crosses the gate and collapses approve -> partition -> next-stage."""
    plan = str(fixtures_dir / "plan_two_stage.toml")
    _start(store, "d2")
    d = cli.cmd_drive(_drive_ns("d2", plan=plan, approved_by="Fedor", **SUBSTANTIVE), store=store)
    assert d.node == Node.EXECUTING.value
    assert d.action == "dispatch"  # first stage is spawn:developer
    cmds = [c["command"] for c in d.data["trace"]]
    assert cmds == ["classify", "plan", "submit_plan", "approve", "partition", "next_stage"]


def test_drive_idempotent_at_executing(store, fixtures_dir):
    """Re-running drive once already at EXECUTING is a no-op reporting the node."""
    plan = str(fixtures_dir / "plan_two_stage.toml")
    _start(store, "d3")
    cli.cmd_drive(_drive_ns("d3", plan=plan, approved_by="Fedor", **SUBSTANTIVE), store=store)
    d = cli.cmd_drive(_drive_ns("d3", plan=plan, approved_by="Fedor", **SUBSTANTIVE), store=store)
    assert d.ok
    assert d.action == "noop"
    assert d.node == Node.EXECUTING.value
    assert d.data["trace"] == []


def test_drive_small_change_to_executing(store):
    """A small change has no plan gate; drive runs straight to EXECUTING in-thread."""
    _start(store, "d4")
    d = cli.cmd_drive(_drive_ns("d4", changed_lines=3, files=1), store=store)
    assert d.node == Node.EXECUTING.value
    assert d.action == "execute_in_thread"


def test_drive_chat_is_terminal(store):
    """A chat classifies terminal at ROUTED; drive answers in-thread, no gate."""
    _start(store, "d5")
    d = cli.cmd_drive(_drive_ns("d5", chat=True), store=store)
    assert d.node == Node.ROUTED.value
    assert d.action == "answer_in_thread"


def test_drive_substantive_without_plan_stops(store):
    """Substantive drive with no --plan can reach PLANNING but cannot submit; it stops."""
    _start(store, "d6")
    d = cli.cmd_drive(_drive_ns("d6", **SUBSTANTIVE), store=store)
    assert d.ok is False
    assert d.node == Node.PLANNING.value
    assert d.action == "fix_plan"


# --- close ------------------------------------------------------------------

def _drive_to_executing(store, sid, fixtures_dir):
    plan = str(fixtures_dir / "plan_two_stage.toml")
    _start(store, sid)
    cli.cmd_drive(_drive_ns(sid, plan=plan, approved_by="Fedor", **SUBSTANTIVE), store=store)


def test_close_blocked_on_unpassed_stage(store, fixtures_dir):
    """Recording stage 1 leaves stage 2 pending; close reports more-stages-remain and
    does NOT fabricate the rest or reach resolution."""
    _drive_to_executing(store, "c1", fixtures_dir)
    d = cli.cmd_close(_close_ns("c1", status="passed", control="reviewed: ok"), store=store)
    assert d.action == "next_stage"
    assert "more stages remain" in d.detail
    assert store.load("c1").node == Node.VERIFYING.value


def test_close_blocked_on_plugin_phase(store, fixtures_dir):
    """With all stages passed but the experience plugin's phases unrecorded, close's
    resolve-probe stays at RESOLUTION and surfaces the [experience] blocker."""
    _drive_to_executing(store, "c2", fixtures_dir)
    # stage 1
    cli.cmd_close(_close_ns("c2", status="passed", control="reviewed: ok"), store=store)
    # advance to stage 2 and record it
    cli.cmd_next_stage(ns(session="c2"), store=store)
    cli.cmd_close(_close_ns("c2", status="passed", control="reviewed: ok"), store=store)
    # now at RESOLUTION; close without a confirmer probes resolve and reports blockers
    d = cli.cmd_close(_close_ns("c2"), store=store)
    assert d.ok is False
    assert d.node == Node.RESOLUTION.value
    assert any("experience" in b for b in d.data["blockers"])
    assert store.load("c2").node == Node.RESOLUTION.value  # probe did not transition


def test_close_resolves_when_clean(store, fixtures_dir):
    """Experience phases recorded + an explicit --confirmed-by resolves the session."""
    _drive_to_executing(store, "c3", fixtures_dir)
    cli.cmd_close(_close_ns("c3", status="passed", control="reviewed: ok"), store=store)
    cli.cmd_next_stage(ns(session="c3"), store=store)
    cli.cmd_close(_close_ns("c3", status="passed", control="reviewed: ok"), store=store)
    cli.cmd_plugin_record(ns(session="c3", plugin="experience", phase="searched", note=None), store=store)
    cli.cmd_plugin_record(ns(session="c3", plugin="experience", phase="recorded", note=None), store=store)
    d = cli.cmd_close(_close_ns("c3", confirmed_by="Fedor"), store=store)
    assert d.node == Node.RESOLVED.value
    assert d.marker == "COMPLETED"


def test_close_failed_stage_surfaces_diagnosing(store, fixtures_dir):
    """A FAILED stage routes to DIAGNOSING (overcome-difficulty) and close surfaces it
    rather than swallowing it or proceeding toward resolution."""
    _drive_to_executing(store, "c5", fixtures_dir)
    d = cli.cmd_close(_close_ns("c5", status="failed", control="reviewed: ok",
                                actual="boom"), store=store)
    assert d.ok is False
    assert d.node == Node.DIAGNOSING.value
    assert d.action == "declare"
    assert d.marker == "OVERCOME-DIFFICULTY"


def test_close_with_confirmer_but_plugin_blocking(store, fixtures_dir):
    """A non-empty --confirmed-by does not override real resolution blockers: with the
    experience phases unrecorded, close reports fix_stages with the real blockers."""
    _drive_to_executing(store, "c6", fixtures_dir)
    cli.cmd_close(_close_ns("c6", status="passed", control="reviewed: ok"), store=store)
    cli.cmd_next_stage(ns(session="c6"), store=store)
    cli.cmd_close(_close_ns("c6", status="passed", control="reviewed: ok"), store=store)
    d = cli.cmd_close(_close_ns("c6", confirmed_by="Fedor"), store=store)
    assert d.ok is False
    assert d.action == "fix_stages"
    assert any("experience" in b for b in d.data["blockers"])
    assert all("empty confirmer" not in b for b in d.data["blockers"])
    assert store.load("c6").node == Node.RESOLUTION.value


def test_close_ready_without_confirmer_awaits(store):
    """A clean session at RESOLUTION with no plugin blockers and no --confirmed-by
    stops at the resolution gate, awaiting explicit confirmation (gate-stop)."""
    # a small change carries no experience plugin, so the only blocker is the confirmer
    _start(store, "c4")
    cli.cmd_drive(_drive_ns("c4", changed_lines=3, files=1), store=store)
    cli.cmd_close(_close_ns("c4", status="passed"), store=store)  # in_thread stage, no control
    d = cli.cmd_close(_close_ns("c4"), store=store)
    assert d.ok
    assert d.node == Node.RESOLUTION.value
    assert d.action == "await_user_confirmation"
    assert store.load("c4").node == Node.RESOLUTION.value
