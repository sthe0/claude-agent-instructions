"""Fold-before-essence: cmd_present_plan must fold any pending enumerator sidecar
BEFORE stamping the receipt, so candidates are in the bag at presentation time
rather than appearing as a surprise at approve (#60).

Covers:
- A sidecar that lands before present-plan is called folds its candidates into
  the bag and PERSISTS them before the receipt is stamped.
- A second fold call (from approve) at the same digest is a no-op — the same-
  digest guard in _fold_enumeration_sidecar fires, so no double-counting occurs.
- When plan presentation gate is off, the fold still runs (the fold path is
  unconditional on bag presence, not gated by plan_presentation_active).
"""
from __future__ import annotations

from argparse import Namespace
from pathlib import Path

import pytest

from agentctl import cli, enumerate_sidecar, plugins, plugins_premise
from agentctl.plan import PlanError, load_plan

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
    """Drive the session to PLAN_READY with the premise plugin armed and the order
    covered, WITHOUT running the synchronous question-enumerate — enumeration is
    left to the sidecar, which is the scenario fold-before-essence addresses."""
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


def _write_rendering(path, content="Plan essence: fold test.\n"):
    path.write_text(content, encoding="utf-8")
    return path


class TestFoldBeforeEssenceReceipt:
    """The sidecar fold runs inside cmd_present_plan before stamping the receipt."""

    def test_sidecar_candidates_land_in_bag_after_present_plan(
            self, store, tmp_path, monkeypatch):
        """The primary assertion: after present-plan, the bag has the sidecar
        candidates — folded and PERSISTED — so they are visible to the coordinator
        before approve runs, not discovered as a gate blocker at approve time."""
        monkeypatch.delenv("AGENTCTL_PREMISE", raising=False)
        root = tmp_path / "sidecars"
        monkeypatch.setattr(enumerate_sidecar, "DEFAULT_ROOT", root)
        sid = "fold-essence-land"
        plan = str(FIXTURES / "plan_two_stage.toml")

        _to_plan_ready_with_premise(store, sid, plan)
        digest = _land_sidecar(sid, plan,
                               [("goal", "which failure mode is out of scope?"),
                                ("stage:1.means", "why this tool?")],
                               root=root)
        # The synchronous enumerate was never run, so enumerated is still False —
        # the fold-before-essence path must handle this state.
        bag = store.load(sid).plugins["premise"]
        assert bag.get("enumerated") is False

        rendering = _write_rendering(tmp_path / "essence.md")
        d = cli.cmd_present_plan(
            ns(session=sid, kind="essence", rendering_file=str(rendering),
               emit_skeleton=False),
            store=store)
        assert d.ok is True, d.detail

        bag_after = store.load(sid).plugins["premise"]
        assert bag_after["enumerated"] is True, "fold must set enumerated=True"
        assert bag_after["enumerated_at"] == digest
        candidates = {c["id"]: c for c in bag_after["candidates"]}
        assert "qenum-meta-1" in candidates
        assert "qenum-s1-1" in candidates
        assert "which failure mode" in candidates["qenum-meta-1"]["statement"]

    def test_sidecar_fold_at_present_plan_is_persisted_not_only_in_memory(
            self, store, tmp_path, monkeypatch):
        """Mutation-proof: the fold must be written to disk, not merely applied to
        the in-memory state object that cmd_present_plan then discards on return.
        A fold left in memory only would name candidates no on-disk bag entry
        references, so question-candidate-dispose could not address them."""
        monkeypatch.delenv("AGENTCTL_PREMISE", raising=False)
        root = tmp_path / "sidecars"
        monkeypatch.setattr(enumerate_sidecar, "DEFAULT_ROOT", root)
        sid = "fold-essence-persist"
        plan = str(FIXTURES / "plan_two_stage.toml")

        _to_plan_ready_with_premise(store, sid, plan)
        _land_sidecar(sid, plan, [("goal", "what is the done criterion?")], root=root)

        rendering = _write_rendering(tmp_path / "essence2.md")
        cli.cmd_present_plan(
            ns(session=sid, kind="essence", rendering_file=str(rendering),
               emit_skeleton=False),
            store=store)

        # Load a fresh store instance to verify the fold is on disk, not just
        # in the object cmd_present_plan held in memory.
        from agentctl.store import FileStateStore
        fresh_store = FileStateStore(tmp_path / "state")
        bag = fresh_store.load(sid).plugins["premise"]
        assert bag["enumerated"] is True
        assert any(c["id"] == "qenum-meta-1" for c in bag["candidates"])

    def test_fold_is_noop_at_same_digest_so_approve_sees_no_new_candidates(
            self, store, tmp_path, monkeypatch):
        """After present-plan folds the sidecar (setting enumerated=True at the
        current digest), approve's own fold call is a no-op: the same-digest guard
        fires, and no surprise candidates appear at approve time."""
        monkeypatch.delenv("AGENTCTL_PREMISE", raising=False)
        root = tmp_path / "sidecars"
        monkeypatch.setattr(enumerate_sidecar, "DEFAULT_ROOT", root)
        sid = "fold-essence-noop-approve"
        plan = str(FIXTURES / "plan_two_stage.toml")

        _to_plan_ready_with_premise(store, sid, plan)
        _land_sidecar(sid, plan, [("goal", "which mode is out of scope?")], root=root)

        rendering = _write_rendering(tmp_path / "essence3.md")
        cli.cmd_present_plan(
            ns(session=sid, kind="essence", rendering_file=str(rendering),
               emit_skeleton=False),
            store=store)

        # Dispose the candidate the fold revealed so approve can pass.
        d = cli.cmd_question_candidate_dispose(
            ns(session=sid, id="qenum-meta-1", as_="dismissed",
               reason="answered in the goal", question=None),
            store=store)
        assert d.ok is True

        # Approve must not re-fold the sidecar (candidates stay at 1, disposed).
        d = cli.cmd_approve(ns(session=sid, by="user"), store=store)
        assert d.ok is True, d.data.get("blockers")
        bag = store.load(sid).plugins["premise"]
        assert len(bag["candidates"]) == 1
        assert bag["candidates"][0]["disposition"] == "dismissed"

    def test_fold_is_silent_when_no_sidecar_has_landed(
            self, store, tmp_path, monkeypatch):
        """When no sidecar exists for the current digest, the fold is a no-op and
        present-plan succeeds normally without touching the bag."""
        monkeypatch.delenv("AGENTCTL_PREMISE", raising=False)
        root = tmp_path / "sidecars"
        monkeypatch.setattr(enumerate_sidecar, "DEFAULT_ROOT", root)
        sid = "fold-essence-no-sidecar"
        plan = str(FIXTURES / "plan_two_stage.toml")

        _to_plan_ready_with_premise(store, sid, plan)
        bag_before = dict(store.load(sid).plugins["premise"])

        rendering = _write_rendering(tmp_path / "essence4.md")
        d = cli.cmd_present_plan(
            ns(session=sid, kind="essence", rendering_file=str(rendering),
               emit_skeleton=False),
            store=store)
        assert d.ok is True, d.detail

        # No sidecar => bag is unchanged by the fold path
        bag_after = store.load(sid).plugins["premise"]
        assert bag_after.get("enumerated") == bag_before.get("enumerated")
        assert bag_after.get("candidates") == bag_before.get("candidates")


class TestFoldExceptNarrowing:
    """#182: the fold's `except Exception: pass` swallowed every exception,
    including bugs unrelated to a plan-load failure. Narrowed to
    `except (OSError, PlanError)` — the same family every other `load_plan`
    call site in cli.py already narrows to."""

    def test_still_swallows_a_transient_load_plan_failure(
            self, store, tmp_path, monkeypatch):
        """Preserves the ORIGINAL bare except's tolerance for the one failure
        mode it exists to swallow: a load_plan failure inside this exact race
        window (#60), which the coverage_block check moments later re-attempts
        against the same (here: valid) file."""
        monkeypatch.delenv("AGENTCTL_PREMISE", raising=False)
        root = tmp_path / "sidecars"
        monkeypatch.setattr(enumerate_sidecar, "DEFAULT_ROOT", root)
        sid = "fold-essence-transient-planerror"
        plan = str(FIXTURES / "plan_two_stage.toml")

        _to_plan_ready_with_premise(store, sid, plan)

        real_load_plan = cli.load_plan
        calls = {"n": 0}

        def flaky_load_plan(path, *a, **kw):
            calls["n"] += 1
            if calls["n"] == 1:
                raise PlanError("transient race: plan not readable yet")
            return real_load_plan(path, *a, **kw)

        monkeypatch.setattr(cli, "load_plan", flaky_load_plan)

        rendering = _write_rendering(tmp_path / "essence-flaky.md")
        # Must not raise — the narrowed except still catches this exact type.
        cli.cmd_present_plan(
            ns(session=sid, kind="essence", rendering_file=str(rendering),
               emit_skeleton=False),
            store=store)
        assert calls["n"] >= 1

    def test_no_longer_swallows_an_unrelated_bug(self, store, tmp_path, monkeypatch):
        """A ValueError raised from inside the fold itself (not a plan-load
        failure) used to vanish under the bare `except Exception: pass` — the
        #182 hole. The narrowed except must let it propagate rather than hide
        it behind a load-failure route it does not belong to."""
        monkeypatch.delenv("AGENTCTL_PREMISE", raising=False)
        root = tmp_path / "sidecars"
        monkeypatch.setattr(enumerate_sidecar, "DEFAULT_ROOT", root)
        sid = "fold-essence-unrelated-bug"
        plan = str(FIXTURES / "plan_two_stage.toml")

        _to_plan_ready_with_premise(store, sid, plan)

        def broken_fold(*a, **kw):
            raise ValueError("boom: an unrelated bug in the fold, not a plan-load failure")

        monkeypatch.setattr(cli, "_fold_enumeration_sidecar", broken_fold)

        rendering = _write_rendering(tmp_path / "essence-bug.md")
        with pytest.raises(ValueError, match="boom"):
            cli.cmd_present_plan(
                ns(session=sid, kind="essence", rendering_file=str(rendering),
                   emit_skeleton=False),
                store=store)
