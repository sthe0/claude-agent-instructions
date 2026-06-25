"""Full CLASSIFIED -> RESOLVED cycle, driven through cli command functions with a
fake runner so no real `claude -p` is spawned."""
from argparse import Namespace

from agentctl import cli
from agentctl.dispatch import RunResult
from agentctl.state import Node


def fake_runner(argv):
    # spawn-specialist would print a COMPLETED marker; we just succeed.
    assert "--dry-run" in argv  # walkthrough must never spend
    return RunResult(0, stdout="COMPLETED: stage done\n")


def ns(**kw):
    return Namespace(**kw)


def test_full_substantive_cycle(store, fixtures_dir):
    sid = "wf"
    plan = str(fixtures_dir / "plan_two_stage.toml")

    d = cli.cmd_start(ns(session=sid, task="demo-two-stage", goal="g",
                         done_criterion="dc", criterion_type="measurable",
                         recursion_depth=0), store=store)
    assert d.node == Node.CLASSIFIED.value

    d = cli.cmd_classify(ns(session=sid, chat=False, changed_lines=200, files=5,
                            wall_clock_min=60, tracker_key=None, architectural=True,
                            external_effect=False, new_dependency=False,
                            public_api_change=False), store=store)
    assert d.node == Node.ROUTED.value
    assert d.action == "plan"

    d = cli.cmd_plan(ns(session=sid), store=store)
    assert d.node == Node.PLANNING.value

    d = cli.cmd_submit_plan(ns(session=sid, plan=plan), store=store)
    assert d.node == Node.PLAN_READY.value
    assert d.marker == "PLAN-READY"

    d = cli.cmd_approve(ns(session=sid, by="user"), store=store)
    assert d.node == Node.APPROVED.value
    assert d.action == "partition"

    # skipping partition cannot reach EXECUTING — next_stage is refused at APPROVED
    d = cli.cmd_next_stage(ns(session=sid), store=store)
    assert d.ok is False
    assert store.load(sid).node == Node.APPROVED.value

    # partition assessment (M1–M4) between APPROVED and EXECUTING
    d = cli.cmd_partition(ns(session=sid, m1=True, m2=False, m3=True, m4=False,
                             m3_severe=False, m4_severe=False), store=store)
    assert d.node == Node.PARTITIONED.value
    assert d.data["verdict"] == "recommended"
    assert "## Partition" in d.data["section"]

    # stage 1
    d = cli.cmd_next_stage(ns(session=sid), store=store)
    assert d.node == Node.EXECUTING.value
    assert d.action == "dispatch"
    d = cli.cmd_dispatch(ns(session=sid, budget="medium", complexity="medium",
                            dry_run=True), store=store, runner=fake_runner)
    assert d.ok
    d = cli.cmd_record_result(ns(session=sid, status="passed", actual="import ok",
                               control="reviewed: ok"), store=store)
    assert d.node == Node.VERIFYING.value
    assert d.action == "next_stage"

    # stage 2
    d = cli.cmd_next_stage(ns(session=sid), store=store)
    assert d.node == Node.EXECUTING.value
    d = cli.cmd_dispatch(ns(session=sid, budget="medium", complexity="medium",
                            dry_run=True), store=store, runner=fake_runner)
    assert d.ok
    d = cli.cmd_record_result(ns(session=sid, status="passed", actual="tests green",
                               control="reviewed: ok"), store=store)
    assert d.action == "verify_final"

    d = cli.cmd_verify_final(ns(session=sid), store=store)
    assert d.node == Node.RESOLUTION.value

    # experience auto-activates for substantive sessions and gates resolution
    cli.cmd_plugin_record(ns(session=sid, plugin="experience", phase="searched"), store=store)
    cli.cmd_plugin_record(ns(session=sid, plugin="experience", phase="recorded"), store=store)

    d = cli.cmd_resolve(ns(session=sid, by="user"), store=store)
    assert d.node == Node.RESOLVED.value
    assert d.marker == "COMPLETED"

    # persisted final state honors the RESOLVED invariants
    final = store.load(sid)
    assert final.node == Node.RESOLVED.value
    assert final.all_stages_passed()


def test_small_change_skips_plan_gate(store):
    sid = "sc"
    cli.cmd_start(ns(session=sid, task="tiny", goal="fix typo",
                     done_criterion="typo fixed", criterion_type="measurable",
                     recursion_depth=0), store=store)
    d = cli.cmd_classify(ns(session=sid, chat=False, changed_lines=3, files=1,
                            wall_clock_min=0, tracker_key=None, architectural=False,
                            external_effect=False, new_dependency=False,
                            public_api_change=False), store=store)
    assert d.action == "execute_in_thread"

    # no plan/approve needed: go straight to the synthetic stage
    d = cli.cmd_next_stage(ns(session=sid), store=store)
    assert d.node == Node.EXECUTING.value
    assert d.action == "execute_in_thread"  # in-thread executor, not a spawn
    d = cli.cmd_record_result(ns(session=sid, status="passed", actual="done"), store=store)
    assert d.action == "verify_final"
    cli.cmd_verify_final(ns(session=sid), store=store)
    d = cli.cmd_resolve(ns(session=sid, by="user"), store=store)
    assert d.node == Node.RESOLVED.value
