"""CLI-level tests for the `replan_diff` presentation kind (stage 4): the
--plan-only-with-replan_diff refusal on cmd_present_plan, the replan_diff
branch's plan_review_blockers gating and Directive shape, kind-alone
supersede behavior in _record_plan_presentation exercised through the real
CLI path, and --kind on cmd_confirm_delivery. gates.replan_authorization_*
itself is covered by test_replan_authorization.py; this file only exercises
the cli.py surface those gates sit behind."""
from __future__ import annotations

import hashlib
from argparse import Namespace
from pathlib import Path

import pytest

from agentctl import cli
from agentctl.state import AUTHORIZE_REPLAN_MARKER


def ns(**kw) -> Namespace:
    return Namespace(**kw)


@pytest.fixture
def gate_on(monkeypatch):
    monkeypatch.setenv("AGENTCTL_PLAN_PRESENTATION", "1")


def _sha256_file(p) -> str:
    return hashlib.sha256(Path(p).read_bytes()).hexdigest()


def _to_plan_ready(store, sid, plan) -> None:
    cli.cmd_start(
        ns(session=sid, task="demo", goal="", done_criterion="",
           criterion_type="measurable", recursion_depth=0),
        store=store,
    )
    cli.cmd_classify(
        ns(session=sid, chat=False, changed_lines=200, files=5, wall_clock_min=60,
           tracker_key=None, architectural=True, external_effect=False,
           new_dependency=False, public_api_change=False),
        store=store,
    )
    cli.cmd_plan(ns(session=sid), store=store)
    cli.cmd_submit_plan(ns(session=sid, plan=plan), store=store)


def _write_rendering(tmp_path, text, name="rendering.txt") -> str:
    p = tmp_path / name
    p.write_text(text, encoding="utf-8")
    return str(p)


def _pass_review(store, sid, target):
    """Record a passing whole-plan thinker review of `target`, the candidate
    plan file — not necessarily state.plan_path."""
    return cli.cmd_plan_review(
        ns(session=sid, target=target, scope=None, verdict="pass",
           reviewer="thinker", concerns=None, note="",
           plan_digest=_sha256_file(target)),
        store=store,
    )


# --- --plan is refused outright for essence/full -------------------------------

def test_essence_rejects_explicit_plan_arg(store, fixtures_dir, tmp_path, gate_on):
    sid = "rd-e1"
    plan = str(fixtures_dir / "plan_two_stage.toml")
    _to_plan_ready(store, sid, plan)
    rendering = _write_rendering(tmp_path, "Summary.")
    d = cli.cmd_present_plan(
        ns(session=sid, kind="essence", plan=plan, rendering_file=rendering,
           emit_skeleton=False),
        store=store,
    )
    assert d.ok is False
    assert "--plan is only accepted with --kind" in d.detail
    assert "replan_diff" in d.detail
    assert store.load(sid).plan_presentations == []


def test_full_rejects_explicit_plan_arg(store, fixtures_dir, tmp_path, gate_on):
    sid = "rd-f1"
    plan = str(fixtures_dir / "plan_two_stage.toml")
    _to_plan_ready(store, sid, plan)
    rendering = _write_rendering(tmp_path, "[stage 1] Scaffold module\n[stage 2] Add tests\n")
    d = cli.cmd_present_plan(
        ns(session=sid, kind="full", plan=plan, rendering_file=rendering,
           emit_skeleton=False),
        store=store,
    )
    assert d.ok is False
    assert "--plan is only accepted with --kind" in d.detail
    assert store.load(sid).plan_presentations == []


def test_replan_diff_without_explicit_plan_defaults_to_state_plan_path(
        store, fixtures_dir, tmp_path, gate_on, monkeypatch):
    """--plan omitted with --kind replan_diff falls back to state.plan_path,
    exactly as the stage's expected result image describes."""
    monkeypatch.setenv("AGENTCTL_PLAN_REVIEW", "1")
    sid = "rd-default"
    plan = str(fixtures_dir / "plan_two_stage.toml")
    _to_plan_ready(store, sid, plan)
    _pass_review(store, sid, plan)
    rendering = _write_rendering(tmp_path, "Proposed diff.")
    d = cli.cmd_present_plan(
        ns(session=sid, kind="replan_diff", plan=None, rendering_file=rendering,
           emit_skeleton=False),
        store=store,
    )
    assert d.ok is True, d.detail
    receipt = store.load(sid).plan_presentations[0]
    assert receipt.plan_path == plan


# --- replan_diff branch: plan_review_blockers gating + Directive shape --------

def test_replan_diff_blocked_without_review_of_the_candidate(
        store, fixtures_dir, tmp_path, gate_on, monkeypatch):
    monkeypatch.setenv("AGENTCTL_PLAN_REVIEW", "1")
    sid = "rd-r1"
    plan = str(fixtures_dir / "plan_two_stage.toml")
    candidate = str(fixtures_dir / "plan_two_stage_refined.toml")
    _to_plan_ready(store, sid, plan)
    rendering = _write_rendering(tmp_path, "Proposed diff.")

    d = cli.cmd_present_plan(
        ns(session=sid, kind="replan_diff", plan=candidate, rendering_file=rendering,
           emit_skeleton=False),
        store=store,
    )
    assert d.ok is False
    assert "blockers" in d.data
    assert store.load(sid).plan_presentations == []


def test_replan_diff_allowed_after_reviewing_the_candidate_directive_shape(
        store, fixtures_dir, tmp_path, gate_on, monkeypatch):
    monkeypatch.setenv("AGENTCTL_PLAN_REVIEW", "1")
    sid = "rd-r2"
    plan = str(fixtures_dir / "plan_two_stage.toml")
    candidate = str(fixtures_dir / "plan_two_stage_refined.toml")
    _to_plan_ready(store, sid, plan)
    review = _pass_review(store, sid, candidate)
    assert review.ok is True, review.detail

    rendering = _write_rendering(tmp_path, "Proposed diff.")
    d = cli.cmd_present_plan(
        ns(session=sid, kind="replan_diff", plan=candidate, rendering_file=rendering,
           emit_skeleton=False),
        store=store,
    )
    assert d.ok is True, d.detail
    assert AUTHORIZE_REPLAN_MARKER in d.detail
    assert "sleep 2" in d.detail
    assert "FINAL text" in d.detail
    assert "next turn" in d.detail
    assert d.data["authorize_replan_marker"] == AUTHORIZE_REPLAN_MARKER
    assert isinstance(d.data["next_steps"], list) and len(d.data["next_steps"]) == 3

    receipt = store.load(sid).plan_presentations[0]
    assert receipt.kind == "replan_diff"
    assert receipt.plan_path == candidate
    assert receipt.plan_sha256 == _sha256_file(candidate)


# --- kind-alone supersede, exercised through the real CLI path ----------------

def test_replan_diff_supersedes_by_kind_alone_across_different_candidate_paths(
        store, fixtures_dir, tmp_path, gate_on, monkeypatch):
    monkeypatch.setenv("AGENTCTL_PLAN_REVIEW", "1")
    sid = "rd-sup"
    plan = str(fixtures_dir / "plan_two_stage.toml")
    candidate_a = str(fixtures_dir / "plan_two_stage.toml")
    candidate_b = str(fixtures_dir / "plan_two_stage_refined.toml")
    _to_plan_ready(store, sid, plan)

    _pass_review(store, sid, candidate_a)
    rendering_a = _write_rendering(tmp_path, "Diff A.", name="a.txt")
    d1 = cli.cmd_present_plan(
        ns(session=sid, kind="replan_diff", plan=candidate_a, rendering_file=rendering_a,
           emit_skeleton=False),
        store=store,
    )
    assert d1.ok is True, d1.detail
    assert len(store.load(sid).plan_presentations) == 1

    _pass_review(store, sid, candidate_b)
    rendering_b = _write_rendering(tmp_path, "Diff B.", name="b.txt")
    d2 = cli.cmd_present_plan(
        ns(session=sid, kind="replan_diff", plan=candidate_b, rendering_file=rendering_b,
           emit_skeleton=False),
        store=store,
    )
    assert d2.ok is True, d2.detail

    # kind-alone supersede: the second replan_diff receipt REPLACES the first,
    # even though it targets a completely different candidate path.
    receipts = store.load(sid).plan_presentations
    assert len(receipts) == 1
    assert receipts[0].plan_path == candidate_b
    assert receipts[0].plan_sha256 == _sha256_file(candidate_b)


def test_essence_and_replan_diff_receipts_coexist_independently(
        store, fixtures_dir, tmp_path, gate_on, monkeypatch):
    """essence/full keep their own (plan_path, kind) supersede key — a
    replan_diff presentation must not evict or be evicted by an essence one."""
    monkeypatch.setenv("AGENTCTL_PLAN_REVIEW", "1")
    sid = "rd-coexist"
    plan = str(fixtures_dir / "plan_two_stage.toml")
    candidate = str(fixtures_dir / "plan_two_stage_refined.toml")
    _to_plan_ready(store, sid, plan)

    _pass_review(store, sid, plan)
    essence_rendering = _write_rendering(tmp_path, "Essence.", name="essence.txt")
    d_essence = cli.cmd_present_plan(
        ns(session=sid, kind="essence", plan=None, rendering_file=essence_rendering,
           emit_skeleton=False),
        store=store,
    )
    assert d_essence.ok is True, d_essence.detail

    _pass_review(store, sid, candidate)
    diff_rendering = _write_rendering(tmp_path, "Diff.", name="diff.txt")
    d_diff = cli.cmd_present_plan(
        ns(session=sid, kind="replan_diff", plan=candidate, rendering_file=diff_rendering,
           emit_skeleton=False),
        store=store,
    )
    assert d_diff.ok is True, d_diff.detail

    kinds = sorted(p.kind for p in store.load(sid).plan_presentations)
    assert kinds == ["essence", "replan_diff"]


# --- --kind on cmd_confirm_delivery: a receipt for kind X does not satisfy kind Y

def test_confirm_delivery_replan_diff_not_satisfied_by_essence_receipt(
        store, fixtures_dir, tmp_path, gate_on, monkeypatch):
    monkeypatch.setenv("AGENTCTL_PLAN_REVIEW", "1")
    sid = "cd-mismatch1"
    plan = str(fixtures_dir / "plan_two_stage.toml")
    _to_plan_ready(store, sid, plan)
    _pass_review(store, sid, plan)
    rendering = _write_rendering(tmp_path, "Essence.")
    d_present = cli.cmd_present_plan(
        ns(session=sid, kind="essence", plan=None, rendering_file=rendering,
           emit_skeleton=False),
        store=store,
    )
    assert d_present.ok is True, d_present.detail

    d = cli.cmd_confirm_delivery(
        ns(session=sid, kind="replan_diff", by="fedor", note="hook is dead",
           escape_reason="other"),
        store=store,
    )
    assert d.ok is False
    assert "replan_diff" in d.detail
    assert "no replan_diff presentation receipt" in d.detail


def test_confirm_delivery_essence_not_satisfied_by_replan_diff_receipt(
        store, fixtures_dir, tmp_path, gate_on, monkeypatch):
    monkeypatch.setenv("AGENTCTL_PLAN_REVIEW", "1")
    sid = "cd-mismatch2"
    plan = str(fixtures_dir / "plan_two_stage.toml")
    candidate = str(fixtures_dir / "plan_two_stage_refined.toml")
    _to_plan_ready(store, sid, plan)
    _pass_review(store, sid, candidate)
    rendering = _write_rendering(tmp_path, "Diff.")
    d_present = cli.cmd_present_plan(
        ns(session=sid, kind="replan_diff", plan=candidate, rendering_file=rendering,
           emit_skeleton=False),
        store=store,
    )
    assert d_present.ok is True, d_present.detail

    d = cli.cmd_confirm_delivery(
        ns(session=sid, kind="essence", by="fedor", note="hook is dead",
           escape_reason="other"),
        store=store,
    )
    assert d.ok is False
    assert "no essence presentation receipt" in d.detail


def test_confirm_delivery_kind_default_is_essence_when_omitted(
        store, fixtures_dir, tmp_path, gate_on, monkeypatch):
    """`getattr(args, "kind", None)` defaulting: a caller (or an older test
    Namespace) that never sets --kind must land on essence, matching the
    argparse default, not raise or silently match everything."""
    monkeypatch.setenv("AGENTCTL_PLAN_REVIEW", "1")
    sid = "cd-default"
    plan = str(fixtures_dir / "plan_two_stage.toml")
    candidate = str(fixtures_dir / "plan_two_stage_refined.toml")
    _to_plan_ready(store, sid, plan)
    _pass_review(store, sid, candidate)
    rendering = _write_rendering(tmp_path, "Diff.")
    cli.cmd_present_plan(
        ns(session=sid, kind="replan_diff", plan=candidate, rendering_file=rendering,
           emit_skeleton=False),
        store=store,
    )

    d = cli.cmd_confirm_delivery(
        ns(session=sid, by="fedor", note="hook is dead", escape_reason="other"),
        store=store,
    )
    assert d.ok is False
    assert "no essence presentation receipt" in d.detail


def test_parser_present_plan_accepts_plan_flag_and_confirm_delivery_accepts_kind_flag():
    parser = cli.build_parser()
    args = parser.parse_args(
        ["present-plan", "--session", "s", "--kind", "replan_diff",
         "--plan", "/candidate.toml", "--rendering-file", "/r.txt"])
    assert args.plan == "/candidate.toml"

    args2 = parser.parse_args(
        ["confirm-delivery", "--session", "s", "--kind", "replan_diff",
         "--by", "f", "--note", "n", "--escape-reason", "other"])
    assert args2.kind == "replan_diff"
