"""Stage 4 / R3: scope premise enumeration to the edit area and carry dismissed
questions by content hash.

Two difficulties this closes. First, a narrowed pass used to be gated on the
transient `bag['enumerated']` flag, which `_launch_enumeration` clears on every
submit/replan — so a manual `question-enumerate` run in the window before the
relaunched background worker lands widened to the whole plan for no reason the
per-part baseline didn't already answer. Second, a pair addressed to a stage a
narrowed pass never read had no principled home: writing it as a fresh candidate
would disposition a stage the pass has no standing over, and a coordinator's own
dismissal of a candidate was tied to that candidate's id — an id a later pass's
part/index churn does not preserve, so a re-raised question the coordinator had
already dismissed came back looking new.

Both fixes are exercised here against the real `cli`/`plugins_premise`/`premise`
code, never a re-derivation of the logic under test."""
from __future__ import annotations

from argparse import Namespace
from pathlib import Path
from types import SimpleNamespace

from agentctl import cli, plan, plugins, plugins_premise, premise
from agentctl.plan import load_plan
from agentctl.state import SessionState

FIXTURES = Path(__file__).resolve().parent / "fixtures"


def _runner(stdout, *, returncode=0):
    calls: list[list[str]] = []
    prompts: list[str] = []

    def run(argv, **kw):
        calls.append(argv)
        prompts.append(kw.get("stdin", ""))
        return SimpleNamespace(returncode=returncode, stdout=stdout, stderr="")

    run.calls = calls
    run.prompts = prompts
    return run


_STAGE_TMPL = """\
[[stage]]
index = {i}
title = "Stage {i}"
executor = "spawn:developer"
expected_result_image = "{img}"
criterion_type = "measurable"
done_criterion = "stage {i} done{tail}"
depends_on = {deps}
output_artifacts = ["s{i}.py"]
"""


def _write_plan(path, stages, *, goal="exercise edit-scoped enumeration"):
    body = [
        "[meta]",
        'task_id = "demo-edit-scope"',
        f'goal = "{goal}"',
        'done_criterion = "all stages PASSED"',
        'criterion_type = "measurable"',
        "",
    ]
    prev = None
    for i, img in stages:
        deps = "[]" if prev is None else f"[{prev}]"
        body.append(_STAGE_TMPL.format(i=i, img=img, deps=deps, tail=""))
        prev = i
    path.write_text("\n".join(body), encoding="utf-8")
    return path


def _state(store, sid="s", *, plan_path):
    state = SessionState(session_id=sid, task_id="t")
    plugins.activate(state, "premise")
    state.plan_path = str(plan_path)
    store.save(state)
    return state


def _enumerate(store, sid, run, *, reopen_dismissed=False):
    return cli.cmd_question_enumerate(
        Namespace(session=sid, reopen_dismissed=reopen_dismissed), store=store, runner=run)


def _bag(store, sid="s"):
    return store.load(sid).plugins["premise"]


def _dispose(store, sid, cid, *, reason):
    return cli.cmd_question_candidate_dispose(
        Namespace(session=sid, id=cid, as_="dismissed", reason=reason, question=""),
        store=store)


# --- (a) scope is keyed on the baseline, not on the transient flag --------------

def test_manual_enumerate_after_launch_clear_reads_only_moved_stage(store, tmp_path):
    """`_launch_enumeration` clears `bag['enumerated']` back to False on every
    submit/replan while leaving the per-part baseline as the last landed pass wrote
    it. A manual `question-enumerate` run in that window must still narrow to the
    stage that actually moved — reading the baseline, never the flag `submit_plan`/
    `replan` just cleared. Pre-lever, `enumeration_run_scope` gated on
    `bag.get('enumerated')`, which is False right after the clear, so this pass
    would have widened to the whole plan and the prompt would have carried stage 1's
    done_criterion too."""
    plan_path = _write_plan(tmp_path / "plan.toml", [(1, "img-one"), (2, "img-two")])
    _state(store, plan_path=plan_path)
    _enumerate(store, "s", _runner(""))

    doc = load_plan(plan_path)
    state = store.load("s")
    bag = state.plugins["premise"]
    cli._launch_enumeration(state, bag, doc, plan_path)
    store.save(state)
    assert bag["enumerated"] is False
    assert bag["enumerated_meta_at"] and bag["enumerated_stage_at"]

    _write_plan(plan_path, [(1, "img-one"), (2, "img-two-EDITED")])
    run = _runner("stage:2.result\tis the edited image still checkable?")
    d = _enumerate(store, "s", run)

    prompt = run.prompts[0]
    assert "stage 2 done" in prompt
    assert "stage 1 done" not in prompt
    assert d.data["whole_plan"] is False and d.data["stages"] == [2]
    assert d.data["scope_source"] == "enumeration_baseline"


# --- (b) out-of-scope pairs are listed, not written, then raised once read ------

def test_out_of_scope_pair_is_listed_not_written_to_candidates(store, tmp_path):
    """A narrowed pass has no standing to disposition a stage it never read. A pair
    the advisor addressed to stage 1 while the pass was narrowed to stage 2 must
    surface only as an out-of-scope listing — never as a fresh 'raised' candidate,
    and never counted against a part's digest the pass did not cover. Pre-lever,
    `_apply_enumeration_result` upserted every pair unconditionally, so this pair
    would have landed as an ordinary raised candidate."""
    plan_path = _write_plan(tmp_path / "plan.toml", [(1, "img-one"), (2, "img-two")])
    _state(store, plan_path=plan_path)
    _enumerate(store, "s", _runner(""))

    _write_plan(plan_path, [(1, "img-one"), (2, "img-two-EDITED")])
    run = _runner("stage:1.means\tstill valid?\nstage:2.result\tand now?")
    d = _enumerate(store, "s", run)

    assert d.data["whole_plan"] is False and d.data["stages"] == [2]
    assert d.data["out_of_scope"] == [
        {"target": "stage:1.means", "question": "still valid?",
         "reason": premise.CANDIDATE_OUT_OF_EDIT_SCOPE},
    ]
    targets = {c["target"] for c in _bag(store)["candidates"]}
    assert "stage:1.means" not in targets
    assert "stage:2.result" in targets


def test_out_of_scope_pair_is_raised_when_its_stage_is_read(store, tmp_path):
    """The same pair, once its own stage is the one that moved, is an ordinary
    candidate — the listing is re-evaluated fresh every pass, never a standing
    refusal."""
    plan_path = _write_plan(tmp_path / "plan.toml", [(1, "img-one"), (2, "img-two")])
    _state(store, plan_path=plan_path)
    _enumerate(store, "s", _runner(""))

    _write_plan(plan_path, [(1, "img-one"), (2, "img-two-EDITED")])
    d1 = _enumerate(store, "s", _runner("stage:1.means\tstill valid?"))
    assert d1.data["out_of_scope"] and not any(
        c["target"] == "stage:1.means" for c in _bag(store)["candidates"])

    _write_plan(plan_path, [(1, "img-one-EDITED"), (2, "img-two-EDITED")])
    d2 = _enumerate(store, "s", _runner("stage:1.means\tstill valid?"))

    assert d2.data["whole_plan"] is False and d2.data["stages"] == [1]
    assert d2.data["out_of_scope"] == []
    matches = [c for c in _bag(store)["candidates"] if c["target"] == "stage:1.means"]
    assert len(matches) == 1 and matches[0]["disposition"] == "raised"


# --- carry never overwrites an open candidate with different text --------------

def test_carried_entry_never_overwrites_open_candidate_with_other_text(store, tmp_path):
    """`_upsert_candidate`'s carry branch resolves an id-slot collision by checking
    the STATEMENT first: when a pass shrinks to only the carried question, its
    naturally-computed id (position 1 in this pass's own part list) lands on
    whatever slot is first — here, an OPEN ('raised') candidate from an earlier
    pass with DIFFERENT wording. The carry must never overwrite that open row; it
    must instead find and reuse its OWN prior dismissed row's id (tier 2), no
    matter which slot that now is. Pre-lever there was no carry branch at all: the
    plain same-id upsert would have overwritten the open 'first wording?' row with
    the fresh (non-carried) 'second wording' text — destroying an undispositioned
    candidate and losing the dismissal in one move."""
    plan_path = _write_plan(tmp_path / "plan.toml", [(1, "img-one")])
    _state(store, plan_path=plan_path)
    _enumerate(store, "s", _runner(
        "stage:1.means\tfirst wording?\nstage:1.means\tsecond wording, unrelated?"))
    ids = {c["statement"]: c["id"] for c in _bag(store)["candidates"]}
    assert ids["[stage:1.means] first wording?"] == "qenum-s1-1"
    assert ids["[stage:1.means] second wording, unrelated?"] == "qenum-s1-2"

    _dispose(store, "s", "qenum-s1-2", reason="answered in the order")

    _write_plan(plan_path, [(1, "img-one-EDITED")])
    _enumerate(store, "s", _runner("stage:1.means\tsecond wording, unrelated?"))

    candidates = {c["id"]: c for c in _bag(store)["candidates"]}
    assert candidates["qenum-s1-1"]["statement"] == "[stage:1.means] first wording?"
    assert candidates["qenum-s1-1"]["disposition"] == "raised"
    assert candidates["qenum-s1-2"]["statement"] == "[stage:1.means] second wording, unrelated?"
    assert candidates["qenum-s1-2"]["disposition"] == "dismissed"
    assert candidates["qenum-s1-2"]["reason"] == "carried: answered in the order (from qenum-s1-2)"


# --- (c) a coordinator dismissal carries by content hash, across ids -----------

def test_dismissal_carried_by_content_hash_across_ids(store, tmp_path):
    """A genuine coordinator dismissal is recorded by content hash
    (bag['dismissed_hashes']), so a LATER pass whose part/index churn produces a
    DIFFERENT id for the same question text still lands dismissed, carrying the
    original reason — not freshly raised under its new id. Pre-lever, disposition
    was keyed purely on id/statement match within the SAME part, so a question that
    moved to a different id slot (here: stage 1 shrinks from two candidates to one,
    shifting stage 2's part contents) would have come back 'raised'."""
    plan_path = _write_plan(tmp_path / "plan.toml", [(1, "img-one"), (2, "img-two")])
    _state(store, plan_path=plan_path)
    _enumerate(store, "s", _runner(
        "stage:1.means\twhy this tool?\nstage:1.means\ta second question?"))
    ids = {c["statement"]: c["id"] for c in _bag(store)["candidates"]}
    assert ids["[stage:1.means] a second question?"] == "qenum-s1-2"

    _dispose(store, "s", "qenum-s1-2", reason="answered in the order")

    _write_plan(plan_path, [(1, "img-one-EDITED"), (2, "img-two")])
    _enumerate(store, "s", _runner("stage:1.means\ta second question?"))

    candidates = {c["statement"]: c for c in _bag(store)["candidates"]}
    match = candidates["[stage:1.means] a second question?"]
    assert match["disposition"] == "dismissed"
    assert match["reason"] == "carried: answered in the order (from qenum-s1-2)"
    # Its OWN id is what carries — not the id a plain re-raise would have computed
    # (position 1 in a shrunk pass, i.e. "qenum-s1-1", which is where the OTHER,
    # untouched, open candidate still lives).
    assert match["id"] == "qenum-s1-2"
    assert candidates["[stage:1.means] why this tool?"]["id"] == "qenum-s1-1"
    assert candidates["[stage:1.means] why this tool?"]["disposition"] == "raised"


def test_manual_enumerate_does_not_reraise_dismissed_text(store, tmp_path):
    """A plain `question-enumerate` run — no `--reopen-dismissed` — must honor the
    carry even though `preserve_disposition=False` is exactly the flag that (absent
    the carry lookup) means 'a human asked for a fresh pass, re-raise everything'.
    The carry check runs BEFORE that flag is consulted, so a dismissed text stays
    dismissed on an ordinary manual re-run; only `--reopen-dismissed` reopens it."""
    plan_path = _write_plan(tmp_path / "plan.toml", [(1, "img-one")])
    _state(store, plan_path=plan_path)
    _enumerate(store, "s", _runner("stage:1.means\twhy this tool?"))
    _dispose(store, "s", "qenum-s1-1", reason="answered in the order")

    _write_plan(plan_path, [(1, "img-one-EDITED")])
    d = _enumerate(store, "s", _runner("stage:1.means\twhy this tool?"))

    match = next(c for c in _bag(store)["candidates"] if c["statement"] == "[stage:1.means] why this tool?")
    assert match["disposition"] == "dismissed"
    assert match["id"] in d.data["carried"]

    _write_plan(plan_path, [(1, "img-one-EDITED-AGAIN")])
    d2 = _enumerate(store, "s", _runner("stage:1.means\twhy this tool?"), reopen_dismissed=True)
    match2 = next(c for c in _bag(store)["candidates"] if c["statement"] == "[stage:1.means] why this tool?")
    assert match2["disposition"] == "raised"
    assert d2.data["carried"] == []


# --- (e) the log carries scope_source / out_of_scope / carried ------------------

def test_enumerate_log_records_scope_source_and_carry_counts(store, tmp_path):
    plan_path = _write_plan(tmp_path / "plan.toml", [(1, "img-one"), (2, "img-two")])
    state = _state(store, plan_path=plan_path)
    _enumerate(store, "s", _runner("stage:1.means\twhy this tool?"))
    first_log = store.load("s").history[-1]
    assert first_log["scope_source"] == "whole_plan"
    assert first_log["out_of_scope"] == 0
    assert first_log["carried"] == 0

    _dispose(store, "s", "qenum-s1-1", reason="answered in the order")

    _write_plan(plan_path, [(1, "img-one"), (2, "img-two-EDITED")])
    # The hash is computed on the question text alone (the "[target] " prefix is
    # stripped before hashing), so a dismissed question re-surfacing under a
    # DIFFERENT target still carries — here "why this tool?" reappears addressed to
    # stage 2 instead of stage 1, and it still lands dismissed.
    _enumerate(store, "s", _runner(
        "stage:1.means\tstill unaddressed?\nstage:2.result\twhy this tool?"))
    second_log = store.load("s").history[-1]
    assert second_log["scope_source"] == "enumeration_baseline"
    assert second_log["out_of_scope"] == 1
    assert second_log["carried"] == 1


def test_fold_log_records_scope_source_and_carry_counts(store, tmp_path, monkeypatch):
    """The sidecar fold (`_fold_enumeration_sidecar`) is a separate code path from
    the synchronous command and must log the same three fields on its own — a fold
    that logged only `raised`/`runner_ok` (as it did pre-lever) would leave the
    fold-heavy majority of real sessions with no record of scope/out-of-scope/carry
    at all."""
    from agentctl import enumerate_sidecar

    plan_path = _write_plan(tmp_path / "plan.toml", [(1, "img-one"), (2, "img-two")])
    state = _state(store, plan_path=plan_path)
    _enumerate(store, "s", _runner("stage:1.means\twhy this tool?"))
    _dispose(store, "s", "qenum-s1-1", reason="answered in the order")

    _write_plan(plan_path, [(1, "img-one"), (2, "img-two-EDITED")])
    doc = load_plan(plan_path)
    digest = plugins_premise._plan_content_digest(doc)
    monkeypatch.setattr(
        enumerate_sidecar, "read_discarding_superseded",
        lambda session_id, want_digest: {
            # "why this tool?" reappears addressed to stage 2 (in scope) rather than
            # stage 1 (out of scope this pass) — the hash strips the target prefix,
            # so it still carries the earlier dismissal under its new address.
            "pairs": [["stage:1.means", "still unaddressed?"],
                      ["stage:2.result", "why this tool?"]],
            "runner_ok": True, "stages": [2], "stderr": "",
        } if want_digest == digest else None,
    )

    live = store.load("s")
    mutated = cli._fold_enumeration_sidecar(live, doc, plan_path)
    assert mutated is True
    store.save(live)

    log = store.load("s").history[-1]
    assert log["event"] == "question_enumerate" and log["via"] == "fold"
    assert log["scope_source"] == "enumeration_baseline"
    assert log["out_of_scope"] == 1
    assert log["carried"] == 1


# --- (d) survival across the launch clear and a bag predating the lever --------

def test_dismissed_hashes_survives_launch_clear_and_missing_key_loads_fine(store, tmp_path):
    plan_path = _write_plan(tmp_path / "plan.toml", [(1, "img-one")])
    _state(store, plan_path=plan_path)
    _enumerate(store, "s", _runner("stage:1.means\twhy this tool?"))
    _dispose(store, "s", "qenum-s1-1", reason="answered in the order")

    state = store.load("s")
    bag = state.plugins["premise"]
    assert bag["dismissed_hashes"]
    doc = load_plan(plan_path)
    cli._launch_enumeration(state, bag, doc, plan_path)
    store.save(state)
    assert store.load("s").plugins["premise"]["dismissed_hashes"] == bag["dismissed_hashes"]

    # A bag minted before this lever has no 'dismissed_hashes' key at all — must not
    # KeyError, and must behave as "nothing carried" (the safe, pre-lever direction).
    old_bag = {"candidates": [], "enumerated": False}
    result = cli._apply_enumeration_result(
        old_bag, doc, plan_path, [("stage:1.means", "why this tool?")], True)
    assert result.carried == []
    assert old_bag["candidates"][0]["disposition"] == "raised"
