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

import copy
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


def _dispose(store, sid, cid, *, reason="", as_="dismissed", question=""):
    return cli.cmd_question_candidate_dispose(
        Namespace(session=sid, id=cid, as_=as_, reason=reason, question=question),
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
    """A genuine coordinator dismissal is recorded by content hash AND target
    (bag['dismissed_hashes']), so a LATER pass whose part/index churn produces a
    DIFFERENT id for the same question text, still addressed to the SAME target,
    lands dismissed, carrying the original reason — not freshly raised under its
    new id. The same-target precondition is what makes this a SILENT carry rather
    than a mere hint (see test_reappearance_under_a_different_target_is_a_hint_not_a_carry
    for the cross-target case). Pre-lever, disposition was keyed purely on
    id/statement match within the SAME part, so a question that moved to a
    different id slot (here: stage 1 shrinks from two candidates to one) would have
    come back 'raised'."""
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
    dismissed on an ordinary manual re-run; only `--reopen-dismissed` reopens it —
    and reopening FORGETS the dismissed_hashes record (not merely skips it for one
    pass), so the very NEXT ordinary pass must not silently re-carry a dismissal
    the coordinator just reopened."""
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

    # The critical next step: a PLAIN pass right after the reopen must not silently
    # re-dismiss it. Pre-fix, --reopen-dismissed only skipped honoring the record
    # for its OWN pass without removing it, so this call would have re-carried it.
    _write_plan(plan_path, [(1, "img-one-EDITED-YET-AGAIN")])
    d3 = _enumerate(store, "s", _runner("stage:1.means\twhy this tool?"))
    match3 = next(c for c in _bag(store)["candidates"] if c["statement"] == "[stage:1.means] why this tool?")
    assert match3["disposition"] == "raised"
    assert d3.data["carried"] == []


# --- a same-hash dismissal under a DIFFERENT target is a hint, never a carry ----

def test_reappearance_under_a_different_target_is_a_hint_not_a_carry(store, tmp_path):
    """A dismissal recorded for one target must NOT silently carry to a same-text
    candidate addressed to a DIFFERENT target — text identity alone cannot tell
    "the same question, re-addressed" from "an unrelated stage that happens to
    provoke identical wording". The re-raised candidate stays OPEN ('raised'), its
    reason stamped with a hint pointing at the earlier dismissal, and is counted
    under carried_hint, never carried."""
    plan_path = _write_plan(tmp_path / "plan.toml", [(1, "img-one"), (2, "img-two")])
    _state(store, plan_path=plan_path)
    _enumerate(store, "s", _runner("stage:1.means\twhy this tool?"))
    _dispose(store, "s", "qenum-s1-1", reason="answered in the order")

    source_before = copy.deepcopy(
        next(c for c in _bag(store)["candidates"] if c["id"] == "qenum-s1-1"))

    _write_plan(plan_path, [(1, "img-one"), (2, "img-two-EDITED")])
    d = _enumerate(store, "s", _runner("stage:2.result\twhy this tool?"))

    source_after = next(c for c in _bag(store)["candidates"] if c["id"] == "qenum-s1-1")
    assert source_after == source_before
    match = next(c for c in _bag(store)["candidates"]
                 if c["statement"] == "[stage:2.result] why this tool?")
    assert match["disposition"] == "raised"
    assert match["reason"] == premise.dismissal_hint_note(
        {"reason": "answered in the order", "from_id": "qenum-s1-1", "target": "stage:1.means"})
    assert match["id"] not in d.data["carried"]
    assert match["id"] in d.data["carried_hint"]


# --- disposing as anything other than 'dismissed' forgets the hash --------------

def test_disposing_as_recorded_forgets_the_dismissed_hash(store, tmp_path):
    """Overturning a dismissal — re-dispositioning the SAME candidate as 'recorded'
    instead — must forget its dismissed_hashes record, so a later re-enumeration of
    the same (text, target) pair is freshly raised rather than silently carried
    forward under a ruling that no longer holds."""
    plan_path = _write_plan(tmp_path / "plan.toml", [(1, "img-one")])
    _state(store, plan_path=plan_path)
    _enumerate(store, "s", _runner("stage:1.means\twhy this tool?"))
    _dispose(store, "s", "qenum-s1-1", reason="answered in the order")
    assert _bag(store)["dismissed_hashes"]

    state = store.load("s")
    state.plugins["premise"]["questions"] = [
        {"id": "q1", "target": "stage:1.means", "question": "why this tool, really?"}]
    store.save(state)
    _dispose(store, "s", "qenum-s1-1", as_="recorded", question="q1")

    content_hash = premise.dismissal_hash("[stage:1.means] why this tool?")
    assert premise.dismissed_hash_records(_bag(store).get("dismissed_hashes"), content_hash) == []

    _write_plan(plan_path, [(1, "img-one-EDITED")])
    d = _enumerate(store, "s", _runner("stage:1.means\twhy this tool?"))
    match = next(c for c in _bag(store)["candidates"]
                 if c["statement"] == "[stage:1.means] why this tool?")
    assert match["disposition"] == "raised"
    assert d.data["carried"] == []


# --- finding-5 invariants: carry never disturbs what it must not touch ----------

def test_recorded_disposition_never_converted_by_carry(tmp_path):
    """`_upsert_candidate`'s tier 1 — an existing row at the entry's own id, same
    statement — must NEVER convert a `recorded` row into a carried dismissal, even
    when the text+target also matches a dismissed_hashes record: a human already
    resolved this question by linking it to a real answer, and a stale or
    coincidental hash match must not undo that."""
    plan_path = _write_plan(tmp_path / "plan.toml", [(1, "img-one")])
    doc = load_plan(plan_path)
    bag = {
        "candidates": [{
            "id": "qenum-s1-1", "statement": "[stage:1.means] why this tool?",
            "disposition": "recorded", "reason": "", "question": "q1", "target": "stage:1.means",
        }],
        "dismissed_hashes": {
            premise.dismissal_hash("[stage:1.means] why this tool?"): [
                {"reason": "answered in the order", "from_id": "qenum-old", "target": "stage:1.means"},
            ],
        },
        "enumerated": True,
    }
    cli._apply_enumeration_result(
        bag, doc, plan_path, [("stage:1.means", "why this tool?")], True)

    match = next(c for c in bag["candidates"] if c["id"] == "qenum-s1-1")
    assert match["disposition"] == "recorded"
    assert match["question"] == "q1"


def test_out_of_scope_candidate_is_byte_identical_across_a_narrowed_pass(store, tmp_path):
    """A candidate whose stage a narrowed pass never re-read is not merely "still
    present" afterward (as test_out_of_scope_pair_is_listed_not_written_to_candidates
    checks by target membership) — none of its OWN fields may have moved either.
    Membership alone would not catch a bug that left the id in place but silently
    rewrote e.g. its reason or disposition."""
    plan_path = _write_plan(tmp_path / "plan.toml", [(1, "img-one"), (2, "img-two")])
    _state(store, plan_path=plan_path)
    _enumerate(store, "s", _runner("stage:1.means\tstill valid?"))
    before = dict(next(c for c in _bag(store)["candidates"] if c["id"] == "qenum-s1-1"))

    _write_plan(plan_path, [(1, "img-one"), (2, "img-two-EDITED")])
    _enumerate(store, "s", _runner("stage:1.means\tstill valid?\nstage:2.result\tand now?"))
    after = next(c for c in _bag(store)["candidates"] if c["id"] == "qenum-s1-1")

    assert after == before


# --- (e) the log carries scope_source / out_of_scope / carried ------------------

def test_enumerate_log_records_scope_source_and_carry_counts(store, tmp_path):
    plan_path = _write_plan(tmp_path / "plan.toml", [(1, "img-one"), (2, "img-two")])
    state = _state(store, plan_path=plan_path)
    _enumerate(store, "s", _runner("stage:1.means\twhy this tool?"))
    first_log = store.load("s").history[-1]
    assert first_log["scope_source"] == "whole_plan"
    assert first_log["out_of_scope"] == 0
    assert first_log["carried"] == 0
    assert first_log["carried_hint"] == 0

    _dispose(store, "s", "qenum-s1-1", reason="answered in the order")

    _write_plan(plan_path, [(1, "img-one-EDITED"), (2, "img-two")])
    # A SILENT carry requires the SAME target as the original dismissal — "why
    # this tool?" reappears addressed to stage 1 again, and this pass narrows to
    # stage 1 (the only stage that moved), so it lands dismissed without a fresh
    # coordinator ruling. The stage:2 pair is out of this pass's scope.
    _enumerate(store, "s", _runner(
        "stage:1.means\twhy this tool?\nstage:2.result\tstill unaddressed?"))
    second_log = store.load("s").history[-1]
    assert second_log["scope_source"] == "enumeration_baseline"
    assert second_log["out_of_scope"] == 1
    assert second_log["carried"] == 1
    assert second_log["carried_hint"] == 0


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

    _write_plan(plan_path, [(1, "img-one-EDITED"), (2, "img-two")])
    doc = load_plan(plan_path)
    digest = plugins_premise._plan_content_digest(doc)
    monkeypatch.setattr(
        enumerate_sidecar, "read_discarding_superseded",
        lambda session_id, want_digest: {
            # "why this tool?" reappears addressed to the SAME target (stage 1,
            # the only stage this pass re-read) — a silent carry requires target
            # identity, not just text identity; see the cross-target hint test for
            # why a different target would NOT carry here.
            "pairs": [["stage:1.means", "why this tool?"],
                      ["stage:2.result", "still unaddressed?"]],
            "runner_ok": True, "stages": [1], "stderr": "",
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
    assert log["carried_hint"] == 0


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


def test_legacy_target_less_dismissal_record_only_hints():
    """A dismissed_hashes record written before per-target tracking (a bare
    {reason, from_id} dict) has no target, so it can never match one: the same
    text is raised with a hint naming an unknown target, never carried."""
    h = premise.dismissal_hash("[stage:1.means] why this tool?")
    legacy = {h: {"reason": "answered in the order", "from_id": "qenum-s1-1"}}
    carry, hint = premise.dismissed_hash_lookup(legacy, h, "stage:1.means")
    assert carry is None
    assert hint == legacy[h]
    assert "unknown target" in premise.dismissal_hint_note(hint)


def test_carry_never_reuses_a_dismissed_row_addressed_to_another_target():
    """`_upsert_candidate`'s tier 2 reuses an existing dismissed row only when it is
    addressed to the SAME target. A same-text dismissed row at a different target
    must stay untouched, and the carried entry must land under a fresh id."""
    other = {
        "id": "qenum-s1-1", "statement": "[stage:1.means] why this tool?",
        "disposition": "dismissed", "reason": "answered in the order",
        "question": "", "target": "stage:1.means",
    }
    candidates = [copy.deepcopy(other)]
    entry = {
        "id": "qenum-s2-1", "statement": "[stage:2.result] why this tool?",
        "disposition": "open", "reason": "", "question": "", "target": "stage:2.result",
    }
    landed = cli._upsert_candidate(
        candidates, entry, preserve_disposition=True,
        carry={"reason": "answered in the order", "from_id": "qenum-old", "target": "stage:2.result"},
    )

    assert candidates[0] == other
    assert landed == "qenum-s2-1"
    assert candidates[1]["target"] == "stage:2.result"
    assert candidates[1]["disposition"] == "dismissed"
