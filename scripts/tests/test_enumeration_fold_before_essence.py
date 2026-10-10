"""The enumerator sidecar fold is retired (amendments-2.md E3): `cmd_present_plan`
and `cmd_approve` no longer read a landed sidecar, so a stale one left by an older
engine version neither adds candidates to the bag nor changes any gate.

Before the retirement `cmd_present_plan` folded a pending sidecar before stamping
the receipt (#60), so its candidates were visible at presentation time rather than
appearing as a surprise at approve. The questions now arrive through the review
route (test_norm_staleness_review_questions.py), so this file pins the absence:

- a sidecar that lands before present-plan is left unread — nothing is folded,
  `enumerated` stays False, and the essence is stamped;
- approve after that is not blocked by the unread sidecar and does not fold it;
- the fold helper itself is gone from the CLI module.
"""
from __future__ import annotations

from argparse import Namespace
from pathlib import Path

from agentctl import cli, enumerate_sidecar, plugins_premise
from agentctl.plan import load_plan
from agentctl.render import render_plan_grants

FIXTURES = Path(__file__).resolve().parent / "fixtures"


def ns(**kw):
    return Namespace(**kw)


def _land_sidecar(sid, plan_path, pairs, *, root):
    digest = plugins_premise._plan_content_digest(load_plan(plan_path))
    enumerate_sidecar.write(sid, digest, {
        "runner_ok": True,
        "pairs": [list(p) for p in pairs],
        "stderr": "",
        "content_digest": digest,
        "plan_path": plan_path,
    }, root=root)
    return digest


def _to_plan_ready_with_premise(store, sid, plan):
    cli.cmd_start(ns(session=sid, task="fold-essence-task", goal="",
                     done_criterion="", criterion_type="measurable",
                     recursion_depth=0), store=store)
    cli.cmd_classify(ns(session=sid, chat=False, changed_lines=200, files=5,
                        wall_clock_min=60, tracker_key=None, architectural=True,
                        external_effect=False, new_dependency=False,
                        public_api_change=False), store=store)
    cli.cmd_plan(ns(session=sid), store=store)
    cli.cmd_submit_plan(ns(session=sid, plan=plan), store=store)
    assert "premise" in store.load(sid).plugins
    cli.cmd_order_raise(ns(session=sid, id="O1", element="the task this plan covers"),
                        store=store)
    cli.cmd_order_dispose(ns(session=sid, id="O1", as_="covered", stage=1, reason=""),
                          store=store)


def _write_rendering(path):
    grants_block = render_plan_grants(
        load_plan(str(FIXTURES / "plan_two_stage.toml")), fmt="compact").strip()
    path.write_text("Plan essence: fold test.\n\n" + grants_block, encoding="utf-8")
    return path


def test_fold_helper_is_gone():
    assert not hasattr(cli, "_fold_enumeration_sidecar")
    assert not hasattr(cli, "_launch_enumeration")


def test_landed_sidecar_is_not_folded_at_present_plan(store, tmp_path, monkeypatch):
    monkeypatch.delenv("AGENTCTL_PREMISE", raising=False)
    root = tmp_path / "sidecars"
    monkeypatch.setattr(enumerate_sidecar, "DEFAULT_ROOT", root)
    sid = "fold-essence-retired"
    plan = str(FIXTURES / "plan_two_stage.toml")

    _to_plan_ready_with_premise(store, sid, plan)
    _land_sidecar(sid, plan,
                  [("goal", "which failure mode is out of scope?"),
                   ("stage:1.means", "why this tool?")],
                  root=root)

    d = cli.cmd_present_plan(
        ns(session=sid, kind="essence",
           rendering_file=str(_write_rendering(tmp_path / "essence.md")),
           emit_skeleton=False),
        store=store)
    assert d.ok is True, d.detail

    bag = store.load(sid).plugins["premise"]
    assert bag["enumerated"] is False
    assert bag["candidates"] == []


def test_approve_neither_folds_nor_is_blocked_by_a_landed_sidecar(
        store, tmp_path, monkeypatch):
    monkeypatch.delenv("AGENTCTL_PREMISE", raising=False)
    root = tmp_path / "sidecars"
    monkeypatch.setattr(enumerate_sidecar, "DEFAULT_ROOT", root)
    sid = "fold-essence-approve"
    plan = str(FIXTURES / "plan_two_stage.toml")

    _to_plan_ready_with_premise(store, sid, plan)
    _land_sidecar(sid, plan, [("goal", "which mode is out of scope?")], root=root)

    d = cli.cmd_approve(ns(session=sid, by="user"), store=store)
    assert d.ok is True, d.data.get("blockers")
    assert store.load(sid).plugins["premise"]["candidates"] == []
