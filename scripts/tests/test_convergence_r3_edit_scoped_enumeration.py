"""Stage 4 / R3, as it survives the enumerator's retirement: the `qenum-` upsert that
carries dismissed questions by content hash and lists out-of-scope pairs.

The standalone enumeration these were written for -- its edit-scoped narrowing, its
launch/fold bookkeeping and its log rows -- is retired (amendments-2.md E3), so the
tests that pinned that machinery are gone with it. What stays is the upsert a bag
minted by an earlier engine still goes through, `cli._apply_enumeration_result`:

- a narrowed pass (`parts=(False, {stages})`) has no standing over a stage it never
  read, so a pair addressed to such a stage is listed as out of scope, never written
  as a fresh candidate;
- a coordinator's dismissal is recorded by content hash AND target, so a re-raised
  question the coordinator already dismissed lands dismissed instead of looking new,
  and a same-text pair under another target is only a hint.

Exercised against the real `cli`/`premise` code, never a re-derivation of the logic
under test; the pass itself is supplied as `target<TAB>question` lines, the shape the
retired runner returned."""
from __future__ import annotations

import copy
from argparse import Namespace
from pathlib import Path

from agentctl import cli, plugins, premise
from agentctl.plan import load_plan
from agentctl.state import SessionState

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


def _enumerate(store, sid, stdout, *, parts=None, reopen_dismissed=False):
    """One legacy pass: upsert the pairs in `stdout` the way the retired
    `question-enumerate` did -- no preserved dispositions, the dismissal carry honoured
    unless `reopen_dismissed`."""
    state = store.load(sid)
    pairs = [tuple(line.split("\t", 1)) for line in stdout.splitlines() if line.strip()]
    result = cli._apply_enumeration_result(
        state.plugins["premise"], load_plan(Path(state.plan_path)), state.plan_path,
        pairs, True, parts=parts, honor_dismissed_hashes=not reopen_dismissed)
    store.save(state)
    return result


def _bag(store, sid="s"):
    return store.load(sid).plugins["premise"]


def _dispose(store, sid, cid, *, reason="", as_="dismissed", question=""):
    return cli.cmd_question_candidate_dispose(
        Namespace(session=sid, id=cid, as_=as_, reason=reason, question=question),
        store=store)


# --- out-of-scope pairs are listed, not written, then raised once read ----------

def test_out_of_scope_pair_is_listed_not_written_to_candidates(store, tmp_path):
    """A narrowed pass has no standing to disposition a stage it never read. A pair
    the advisor addressed to stage 1 while the pass was narrowed to stage 2 must
    surface only as an out-of-scope listing -- never as a fresh 'raised' candidate."""
    plan_path = _write_plan(tmp_path / "plan.toml", [(1, "img-one"), (2, "img-two-EDITED")])
    _state(store, plan_path=plan_path)

    result = _enumerate(
        store, "s", "stage:1.result\tstill valid?\nstage:2.result\tand now?",
        parts=(False, {2}))

    assert result.out_of_scope == [
        {"target": "stage:1.result", "question": "still valid?",
         "reason": premise.CANDIDATE_OUT_OF_EDIT_SCOPE},
    ]
    targets = {c["target"] for c in _bag(store)["candidates"]}
    assert "stage:1.result" not in targets
    assert "stage:2.result" in targets


def test_out_of_scope_pair_is_raised_when_its_stage_is_read(store, tmp_path):
    """The same pair, once its own stage is among those read, is an ordinary
    candidate -- the listing is re-evaluated fresh every pass, never a standing
    refusal."""
    plan_path = _write_plan(tmp_path / "plan.toml", [(1, "img-one"), (2, "img-two-EDITED")])
    _state(store, plan_path=plan_path)

    first = _enumerate(store, "s", "stage:1.result\tstill valid?", parts=(False, {2}))
    assert first.out_of_scope and not any(
        c["target"] == "stage:1.result" for c in _bag(store)["candidates"])

    second = _enumerate(store, "s", "stage:1.result\tstill valid?", parts=(False, {1}))

    assert second.out_of_scope == []
    matches = [c for c in _bag(store)["candidates"] if c["target"] == "stage:1.result"]
    assert len(matches) == 1 and matches[0]["disposition"] == "raised"


# --- carry never overwrites an open candidate with different text --------------

def test_carried_entry_never_overwrites_open_candidate_with_other_text(store, tmp_path):
    """`_upsert_candidate`'s carry branch resolves an id-slot collision by checking
    the STATEMENT first: when a pass shrinks to only the carried question, its
    naturally-computed id (position 1 in this pass's own part list) lands on whatever
    slot is first -- here, an OPEN ('raised') candidate with DIFFERENT wording. The
    carry must never overwrite that open row; it must instead find and reuse its OWN
    prior dismissed row's id, no matter which slot that now is."""
    plan_path = _write_plan(tmp_path / "plan.toml", [(1, "img-one")])
    _state(store, plan_path=plan_path)
    _enumerate(
        store, "s", "stage:1.result\tfirst wording?\nstage:1.result\tsecond wording, unrelated?")
    ids = {c["statement"]: c["id"] for c in _bag(store)["candidates"]}
    assert ids["[stage:1.result] first wording?"] == "qenum-s1-1"
    assert ids["[stage:1.result] second wording, unrelated?"] == "qenum-s1-2"

    _dispose(store, "s", "qenum-s1-2", reason="answered in the order")

    _write_plan(plan_path, [(1, "img-one-EDITED")])
    _enumerate(store, "s", "stage:1.result\tsecond wording, unrelated?")

    candidates = {c["id"]: c for c in _bag(store)["candidates"]}
    assert candidates["qenum-s1-1"]["statement"] == "[stage:1.result] first wording?"
    assert candidates["qenum-s1-1"]["disposition"] == "raised"
    assert candidates["qenum-s1-2"]["statement"] == "[stage:1.result] second wording, unrelated?"
    assert candidates["qenum-s1-2"]["disposition"] == "dismissed"
    assert candidates["qenum-s1-2"]["reason"] == "carried: answered in the order (from qenum-s1-2)"


# --- a coordinator dismissal carries by content hash, across ids ---------------

def test_dismissal_carried_by_content_hash_across_ids(store, tmp_path):
    """A genuine coordinator dismissal is recorded by content hash AND target
    (bag['dismissed_hashes']), so a LATER pass whose part/index churn produces a
    DIFFERENT id for the same question text, still addressed to the SAME target,
    lands dismissed, carrying the original reason -- not freshly raised under its new
    id. The same-target precondition is what makes this a SILENT carry rather than a
    mere hint (see test_reappearance_under_a_different_target_is_a_hint_not_a_carry)."""
    plan_path = _write_plan(tmp_path / "plan.toml", [(1, "img-one"), (2, "img-two")])
    _state(store, plan_path=plan_path)
    _enumerate(store, "s", "stage:1.result\twhy this tool?\nstage:1.result\ta second question?")
    ids = {c["statement"]: c["id"] for c in _bag(store)["candidates"]}
    assert ids["[stage:1.result] a second question?"] == "qenum-s1-2"

    _dispose(store, "s", "qenum-s1-2", reason="answered in the order")

    _write_plan(plan_path, [(1, "img-one-EDITED"), (2, "img-two")])
    _enumerate(store, "s", "stage:1.result\ta second question?", parts=(False, {1}))

    candidates = {c["statement"]: c for c in _bag(store)["candidates"]}
    match = candidates["[stage:1.result] a second question?"]
    assert match["disposition"] == "dismissed"
    assert match["reason"] == "carried: answered in the order (from qenum-s1-2)"
    # Its OWN id is what carries -- not the id a plain re-raise would have computed
    # (position 1 in a shrunk pass, i.e. "qenum-s1-1", which is where the OTHER,
    # untouched, open candidate still lives).
    assert match["id"] == "qenum-s1-2"
    assert candidates["[stage:1.result] why this tool?"]["id"] == "qenum-s1-1"
    assert candidates["[stage:1.result] why this tool?"]["disposition"] == "raised"


def test_manual_enumerate_does_not_reraise_dismissed_text(store, tmp_path):
    """A plain pass -- no `reopen_dismissed` -- must honor the carry even though
    `preserve_disposition=False` is exactly the flag that (absent the carry lookup)
    means 'a human asked for a fresh pass, re-raise everything'. The carry check runs
    BEFORE that flag is consulted, so a dismissed text stays dismissed on an ordinary
    re-run; only reopening reopens it -- and reopening FORGETS the dismissed_hashes
    record (not merely skips it for one pass), so the very NEXT ordinary pass must not
    silently re-carry a dismissal the coordinator just reopened."""
    plan_path = _write_plan(tmp_path / "plan.toml", [(1, "img-one")])
    _state(store, plan_path=plan_path)
    _enumerate(store, "s", "stage:1.result\twhy this tool?")
    _dispose(store, "s", "qenum-s1-1", reason="answered in the order")

    _write_plan(plan_path, [(1, "img-one-EDITED")])
    d = _enumerate(store, "s", "stage:1.result\twhy this tool?")

    match = next(c for c in _bag(store)["candidates"]
                 if c["statement"] == "[stage:1.result] why this tool?")
    assert match["disposition"] == "dismissed"
    assert match["id"] in d.carried

    _write_plan(plan_path, [(1, "img-one-EDITED-AGAIN")])
    d2 = _enumerate(store, "s", "stage:1.result\twhy this tool?", reopen_dismissed=True)
    match2 = next(c for c in _bag(store)["candidates"]
                  if c["statement"] == "[stage:1.result] why this tool?")
    assert match2["disposition"] == "raised"
    assert d2.carried == []

    _write_plan(plan_path, [(1, "img-one-EDITED-YET-AGAIN")])
    d3 = _enumerate(store, "s", "stage:1.result\twhy this tool?")
    match3 = next(c for c in _bag(store)["candidates"]
                  if c["statement"] == "[stage:1.result] why this tool?")
    assert match3["disposition"] == "raised"
    assert d3.carried == []


# --- a same-hash dismissal under a DIFFERENT target is a hint, never a carry ----

def test_reappearance_under_a_different_target_is_a_hint_not_a_carry(store, tmp_path):
    """A dismissal recorded for one target must NOT silently carry to a same-text
    candidate addressed to a DIFFERENT target -- text identity alone cannot tell "the
    same question, re-addressed" from "an unrelated stage that happens to provoke
    identical wording". The re-raised candidate stays OPEN ('raised'), its reason
    stamped with a hint pointing at the earlier dismissal, and is counted under
    carried_hint, never carried."""
    plan_path = _write_plan(tmp_path / "plan.toml", [(1, "img-one"), (2, "img-two")])
    _state(store, plan_path=plan_path)
    _enumerate(store, "s", "stage:1.result\twhy this tool?")
    _dispose(store, "s", "qenum-s1-1", reason="answered in the order")

    source_before = copy.deepcopy(
        next(c for c in _bag(store)["candidates"] if c["id"] == "qenum-s1-1"))

    _write_plan(plan_path, [(1, "img-one"), (2, "img-two-EDITED")])
    d = _enumerate(store, "s", "stage:2.result\twhy this tool?", parts=(False, {2}))

    source_after = next(c for c in _bag(store)["candidates"] if c["id"] == "qenum-s1-1")
    assert source_after == source_before
    match = next(c for c in _bag(store)["candidates"]
                 if c["statement"] == "[stage:2.result] why this tool?")
    assert match["disposition"] == "raised"
    assert match["reason"] == premise.dismissal_hint_note(
        {"reason": "answered in the order", "from_id": "qenum-s1-1", "target": "stage:1.result"})
    assert match["id"] not in d.carried
    assert match["id"] in d.carried_hint


# --- disposing as anything other than 'dismissed' forgets the hash --------------

def test_disposing_as_recorded_forgets_the_dismissed_hash(store, tmp_path):
    """Overturning a dismissal -- re-dispositioning the SAME candidate as 'recorded'
    instead -- must forget its dismissed_hashes record, so a later re-enumeration of
    the same (text, target) pair is freshly raised rather than silently carried
    forward under a ruling that no longer holds."""
    plan_path = _write_plan(tmp_path / "plan.toml", [(1, "img-one")])
    _state(store, plan_path=plan_path)
    _enumerate(store, "s", "stage:1.result\twhy this tool?")
    _dispose(store, "s", "qenum-s1-1", reason="answered in the order")
    assert _bag(store)["dismissed_hashes"]

    state = store.load("s")
    state.plugins["premise"]["questions"] = [
        {"id": "q1", "target": "stage:1.result", "question": "why this tool, really?"}]
    store.save(state)
    _dispose(store, "s", "qenum-s1-1", as_="recorded", question="q1")

    content_hash = premise.dismissal_hash("[stage:1.result] why this tool?")
    assert premise.dismissed_hash_records(_bag(store).get("dismissed_hashes"), content_hash) == []

    _write_plan(plan_path, [(1, "img-one-EDITED")])
    d = _enumerate(store, "s", "stage:1.result\twhy this tool?")
    match = next(c for c in _bag(store)["candidates"]
                 if c["statement"] == "[stage:1.result] why this tool?")
    assert match["disposition"] == "raised"
    assert d.carried == []


# --- carry never disturbs what it must not touch --------------------------------

def test_recorded_disposition_never_converted_by_carry(tmp_path):
    """`_upsert_candidate`'s tier 1 -- an existing row at the entry's own id, same
    statement -- must NEVER convert a `recorded` row into a carried dismissal, even
    when the text+target also matches a dismissed_hashes record: a human already
    resolved this question by linking it to a real answer, and a stale or coincidental
    hash match must not undo that."""
    plan_path = _write_plan(tmp_path / "plan.toml", [(1, "img-one")])
    doc = load_plan(plan_path)
    bag = {
        "candidates": [{
            "id": "qenum-s1-1", "statement": "[stage:1.result] why this tool?",
            "disposition": "recorded", "reason": "", "question": "q1", "target": "stage:1.result",
        }],
        "dismissed_hashes": {
            premise.dismissal_hash("[stage:1.result] why this tool?"): [
                {"reason": "answered in the order", "from_id": "qenum-old", "target": "stage:1.result"},
            ],
        },
        "enumerated": True,
    }
    cli._apply_enumeration_result(
        bag, doc, plan_path, [("stage:1.result", "why this tool?")], True)

    match = next(c for c in bag["candidates"] if c["id"] == "qenum-s1-1")
    assert match["disposition"] == "recorded"
    assert match["question"] == "q1"


def test_out_of_scope_candidate_is_byte_identical_across_a_narrowed_pass(store, tmp_path):
    """A candidate whose stage a narrowed pass never re-read is not merely "still
    present" afterward -- none of its OWN fields may have moved either. Membership
    alone would not catch a bug that left the id in place but silently rewrote e.g. its
    reason or disposition."""
    plan_path = _write_plan(tmp_path / "plan.toml", [(1, "img-one"), (2, "img-two")])
    _state(store, plan_path=plan_path)
    _enumerate(store, "s", "stage:1.result\tstill valid?")
    before = dict(next(c for c in _bag(store)["candidates"] if c["id"] == "qenum-s1-1"))

    _write_plan(plan_path, [(1, "img-one"), (2, "img-two-EDITED")])
    _enumerate(
        store, "s", "stage:1.result\tstill valid?\nstage:2.result\tand now?", parts=(False, {2}))
    after = next(c for c in _bag(store)["candidates"] if c["id"] == "qenum-s1-1")

    assert after == before


# --- a bag predating the dismissed-hash record ----------------------------------

def test_a_bag_without_dismissed_hashes_loads_and_carries_nothing(tmp_path):
    """A bag minted before the carry existed has no 'dismissed_hashes' key at all --
    must not KeyError, and must behave as "nothing carried" (the safe direction)."""
    plan_path = _write_plan(tmp_path / "plan.toml", [(1, "img-one")])
    doc = load_plan(plan_path)
    old_bag = {"candidates": [], "enumerated": False}
    result = cli._apply_enumeration_result(
        old_bag, doc, plan_path, [("stage:1.result", "why this tool?")], True)
    assert result.carried == []
    assert old_bag["candidates"][0]["disposition"] == "raised"


def test_legacy_target_less_dismissal_record_only_hints():
    """A dismissed_hashes record written before per-target tracking (a bare
    {reason, from_id} dict) has no target, so it can never match one: the same
    text is raised with a hint naming an unknown target, never carried."""
    h = premise.dismissal_hash("[stage:1.result] why this tool?")
    legacy = {h: {"reason": "answered in the order", "from_id": "qenum-s1-1"}}
    carry, hint = premise.dismissed_hash_lookup(legacy, h, "stage:1.result")
    assert carry is None
    assert hint == legacy[h]
    assert "unknown target" in premise.dismissal_hint_note(hint)


def test_carry_never_reuses_a_dismissed_row_addressed_to_another_target():
    """`_upsert_candidate`'s tier 2 reuses an existing dismissed row only when it is
    addressed to the SAME target. A same-text dismissed row at a different target
    must stay untouched, and the carried entry must land under a fresh id."""
    other = {
        "id": "qenum-s1-1", "statement": "[stage:1.result] why this tool?",
        "disposition": "dismissed", "reason": "answered in the order",
        "question": "", "target": "stage:1.result",
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
