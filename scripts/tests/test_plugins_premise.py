"""The question-provenance plugin + stage_question_key: the plan_approval-gate arm.

Proves three things the ledger plugin's `resolution` arm has no analogue for:

  1. `plan.stage_question_key` is a per-stage digest covering the FULL stage
     definition INCLUDING the principle and supplies (which stage_carry_key omits),
     and it is invariant to an edit of any OTHER stage — so a question bound to
     `stage:<n>.principle` is invalidated exactly when stage n's principle changes,
     never by an unrelated stage edit.
  2. `premise` auto-activates for EVERY SUBSTANTIVE session on weight_class ALONE —
     the gap-2 arming fix — so an ordinary engineering plan (deliverable_kind unset)
     still gets the gate, unlike the reasoning-only ledger plugin.
  3. the `plan_approval` gate blocks while any question is open or the enumeration
     cross-check has not run against the CURRENT plan content, and a TOML
     comment-only edit (invisible to tomllib) does not re-block a discharged
     enumeration.
"""
from __future__ import annotations

import time
from argparse import Namespace

import pytest

from agentctl import cli, gates, plan, plugins, premise
from agentctl import plugins_premise as pp
from agentctl.state import (
    Actor,
    Criterion,
    Means,
    Principle,
    SessionState,
    Stage,
    Subject,
    Supply,
    WeightClass,
)


@pytest.fixture(autouse=True)
def _premise_armed(monkeypatch):
    """Override conftest's suite-wide AGENTCTL_PREMISE=0 force-off: this module is
    the one place that exercises the real arming predicate, so it deletes the knob
    and lets the plain weight_class logic decide (substantive arms, small-change
    does not). A module-local autouse fixture runs after the conftest one, so this
    delenv wins for every test here."""
    monkeypatch.delenv("AGENTCTL_PREMISE", raising=False)


def _new_state(sid="s", **kw):
    return SessionState(session_id=sid, task_id="t", **kw)


def _cover_the_order(state, stage=1):
    """Seed one covered order element. The gate's order-coverage half fail-closes on
    an EMPTY order bag once a plan is submitted, so a test isolating a QUESTION-side
    blocker (or asserting the gate is otherwise clear) must satisfy it — the order
    half's own two-directional proof lives in test_order_coverage.py."""
    state.plugins["premise"]["order_elements"] = [{
        "id": "O1", "element": "the order this plan answers",
        "disposition": "covered", "stage": stage, "reason": "",
    }]


def _stage(*, index=1, title="Scaffold", principle=None, supplies=(), method="build it"):
    """A fully-populated Stage for exercising stage_question_key. Every field the
    key reads is set explicitly so a test can mutate exactly one and observe."""
    return Stage(
        index=index,
        title=title,
        subject=Subject(material="spec", result="module exists", invariants="imports stay clean"),
        means=Means(means="Edit mod.py", method=method),
        actor=Actor(executor="spawn:developer", capability_required="python"),
        criterion=Criterion(
            criterion_type="measurable",
            done_criterion="pytest green",
            verify_command="pytest -q",
            expected_exit=0,
        ),
        principle=principle,
        conditions="repo checked out",
        supplies=list(supplies),
    )


def _principle(statement="idempotent registration is safe",
               derivation="the docstring says a re-register replaces last-wins, so idempotence holds"):
    return Principle(
        statement=statement,
        source="plugins.register docstring",
        derivation=derivation,
        confidence="high",
        refutation="a second register raised instead of last-wins",
    )


# --- stage_question_key: covers the principle, stable across unrelated edits ----

def test_stage_question_key_changes_when_principle_changes():
    base = _stage(principle=_principle("idempotent registration is safe"))
    changed = _stage(principle=_principle("registration must raise on a duplicate name"))
    assert plan.stage_question_key(base) != plan.stage_question_key(changed)
    # and it is deterministic for identical content (survives being recomputed)
    assert plan.stage_question_key(base) == plan.stage_question_key(
        _stage(principle=_principle("idempotent registration is safe"))
    )


def test_stage_question_key_changes_when_principle_derivation_changes():
    # A derivation-only rewrite must move the key: a question bound to stage:<n>.principle
    # was answered against the OLD inference and must be re-examined when it changes.
    base = _stage(principle=_principle(derivation="the docstring states last-wins, so idempotence holds"))
    changed = _stage(principle=_principle(derivation="the type signature guarantees no duplicate, so idempotence holds"))
    assert plan.stage_question_key(base) != plan.stage_question_key(changed)


def test_stage_question_key_stable_across_unrelated_stage_edit():
    # stage 1 is fixed; stage 2 is edited. stage 1's key must not move, because the key
    # is a per-stage digest — an unrelated stage's edit cannot invalidate a question
    # bound to THIS stage. (F6: only the OWN bound stage's key matters.)
    stage1 = _stage(index=1, title="Scaffold", principle=_principle())
    before = plan.stage_question_key(stage1)

    # two genuinely different sibling stage-2 definitions — differing in title, method,
    # principle and supplies. The key IS content-sensitive (it tells them apart)...
    sib_a = _stage(index=2, title="Add tests", method="write pytest", principle=_principle("x"))
    sib_b = _stage(index=2, title="Add tests (revised)", method="write more pytest",
                   principle=_principle("y"), supplies=[Supply(on=1, element="result")])
    assert plan.stage_question_key(sib_a) != plan.stage_question_key(sib_b)

    # ...yet neither sibling's content feeds stage 1's digest, so editing stage 2
    # (sib_a -> sib_b) never moves stage 1's key.
    assert plan.stage_question_key(stage1) == before


# --- auto-activation: SUBSTANTIVE alone, deliverable_kind irrelevant (gap-2 fix) -

def _classify(store, sid, *, weight_kwargs, chat=False, architectural=True):
    cli.cmd_start(Namespace(session=sid, task="demo", goal="g", done_criterion="dc",
                            criterion_type="measurable", recursion_depth=0), store=store)
    return cli.cmd_classify(Namespace(
        session=sid, chat=chat, tracker_key=None, architectural=architectural,
        external_effect=False, new_dependency=False, public_api_change=False,
        deliverable_kind="", **weight_kwargs,
    ), store=store)


def test_auto_activates_for_substantive_without_deliverable_kind(store):
    # unit: the predicate fires on weight_class alone, with deliverable_kind unset
    substantive = _new_state(weight_class=WeightClass.SUBSTANTIVE.value, deliverable_kind="")
    assert pp._auto_activate(substantive) is True

    # e2e through classify: a SUBSTANTIVE session that never names a deliverable_kind
    # still gets a premise bag (the arming gap the ledger plugin left open).
    _classify(store, "pr-sub", weight_kwargs=dict(changed_lines=200, files=5, wall_clock_min=60))
    state = store.load("pr-sub")
    assert state.weight_class == WeightClass.SUBSTANTIVE.value
    assert state.deliverable_kind == ""
    assert "premise" in state.plugins
    assert state.plugins["premise"]["enumerated"] is False


def test_does_not_auto_activate_for_small_change(store):
    # unit: a SMALL_CHANGE session is never armed
    small = _new_state(weight_class=WeightClass.SMALL_CHANGE.value, deliverable_kind="")
    assert pp._auto_activate(small) is False

    # e2e: a change small enough to route SMALL_CHANGE gets no premise bag
    _classify(store, "pr-small", chat=False, architectural=False,
              weight_kwargs=dict(changed_lines=5, files=1, wall_clock_min=5))
    state = store.load("pr-small")
    assert state.weight_class == WeightClass.SMALL_CHANGE.value
    assert "premise" not in state.plugins


# --- the plan_approval gate blocks on an open question / un-run enumeration ------

def test_gate_blocks_open_question():
    state = _new_state()
    plugins.activate(state, "premise")
    # isolate the OPEN-question blocker from the enumeration blocker
    state.plugins["premise"]["enumerated"] = True
    state.plugins["premise"]["questions"] = [
        {"id": "q1", "target": "plan.goal", "question": "is the goal even reachable?"},
    ]
    blockers = plugins.plugin_gate_blockers(state, "plan_approval")
    assert blockers
    assert any("open" in b for b in blockers)
    # closing it (disposed as assumed with its required fields) clears the gate
    state.plugins["premise"]["questions"] = [
        {"id": "q1", "target": "plan.goal", "question": "is the goal even reachable?",
         "disposition": "assumed", "own_research": "read the tracker thread",
         "basis": "confirmed reachable by the reporter", "risk": "reporter may be wrong"},
    ]
    assert plugins.plugin_gate_blockers(state, "plan_approval") == []


def test_gate_blocks_when_not_enumerated():
    state = _new_state()
    plugins.activate(state, "premise")  # fresh bag: enumerated=False, no questions
    blockers = plugins.plugin_gate_blockers(state, "plan_approval")
    assert blockers == [f"[premise] {pp._ENUMERATE_NOT_RUN}"]
    # the same gate name that the ledger plugin gates (resolution) is untouched here
    assert plugins.plugin_gate_blockers(state, "resolution") == []


def test_gate_blocks_stale_enumerated_at(fixtures_dir):
    # with a submitted plan, an enumeration that ran against DIFFERENT content is stale
    plan_path = str(fixtures_dir / "plan_two_stage.toml")
    state = _new_state(plan_path=plan_path)
    plugins.activate(state, "premise")
    _cover_the_order(state)
    state.plugins["premise"]["enumerated"] = True
    state.plugins["premise"]["enumerated_at"] = "a-digest-of-some-earlier-plan"
    blockers = plugins.plugin_gate_blockers(state, "plan_approval")
    assert blockers == [f"[premise] {pp._ENUMERATE_STALE}"]

    # stamping the enumeration against the CURRENT content clears the gate
    current = pp._plan_content_digest(plan.load_plan(plan_path))
    state.plugins["premise"]["enumerated_at"] = current
    assert plugins.plugin_gate_blockers(state, "plan_approval") == []


def test_gate_blocks_when_covered_stage_content_changes(tmp_path, fixtures_dir):
    """An order element marked 'covered' by stage 1 is invalidated when stage 1's
    content changes on replan (#123) — the order-coverage twin of
    test_stage_question_key_changes_when_principle_changes, run through the same
    gate `test_gate_blocks_stale_enumerated_at` exercises."""
    src = (fixtures_dir / "plan_two_stage.toml").read_text(encoding="utf-8")
    plan_path = tmp_path / "plan.toml"
    plan_path.write_text(src, encoding="utf-8")
    doc = plan.load_plan(str(plan_path))
    current_key = plan.stage_element_keys(doc.stages[0])[premise.WHOLE_STAGE_ELEMENT]

    state = _new_state(plan_path=str(plan_path))
    plugins.activate(state, "premise")
    state.plugins["premise"]["enumerated"] = True
    state.plugins["premise"]["enumerated_at"] = pp._plan_content_digest(doc)
    state.plugins["premise"]["order_elements"] = [{
        "id": "O1", "element": "the order this plan answers",
        "disposition": "covered", "stage": 1, "reason": "",
        "content_digest": current_key,
    }]
    assert plugins.plugin_gate_blockers(state, "plan_approval") == []

    # rewrite stage 1's title — moves stage 1's key, leaves stage 2's untouched
    edited_src = src.replace('title = "Scaffold module"', 'title = "Scaffold module (revised)"')
    plan_path.write_text(edited_src, encoding="utf-8")
    edited_doc = plan.load_plan(str(plan_path))
    state.plugins["premise"]["enumerated_at"] = pp._plan_content_digest(edited_doc)

    blockers = plugins.plugin_gate_blockers(state, "plan_approval")
    assert any("O1" in b and "stage 1" in b and "changed" in b for b in blockers)


def test_comment_only_plan_edit_does_not_reblock_enumeration(tmp_path, fixtures_dir):
    # a TOML comment is invisible to tomllib, so a comment-only edit leaves the
    # content digest byte-identical — a discharged enumeration must not re-block.
    src = (fixtures_dir / "plan_two_stage.toml").read_text(encoding="utf-8")
    plan_path = tmp_path / "plan.toml"
    plan_path.write_text(src, encoding="utf-8")

    digest_before = pp._plan_content_digest(plan.load_plan(str(plan_path)))
    state = _new_state(plan_path=str(plan_path))
    plugins.activate(state, "premise")
    _cover_the_order(state)
    state.plugins["premise"]["enumerated"] = True
    state.plugins["premise"]["enumerated_at"] = digest_before
    assert plugins.plugin_gate_blockers(state, "plan_approval") == []

    # append a pure comment line and reload — tomllib drops it, so the digest holds
    plan_path.write_text(src + "\n# a note for a future reader, no field change\n", encoding="utf-8")
    digest_after = pp._plan_content_digest(plan.load_plan(str(plan_path)))
    assert digest_after == digest_before
    blockers = plugins.plugin_gate_blockers(state, "plan_approval")
    assert not any(pp._ENUMERATE_STALE in b for b in blockers)
    assert blockers == []


# --- the order is plan content, so an order-only edit is a content change -------

_ORDER = """
[meta.order]
customer_id = "user"
customer = "the position that posed the critique task"
functional_place = "the norm governing an act of activity in this engine"
requirements = [
  { id = "R1", text = "the order is a typed object, not one free-text string" },
]

[meta.order.coverage]
R1 = ["stage 1 verify_command"]
"""


def _with_order(tmp_path, fixtures_dir, name, order_toml=_ORDER):
    """The two-stage fixture with a [meta.order] appended. Appended rather than fixtured
    because what these tests need is TWO plans differing in the order ALONE — a second
    committed fixture would differ in whatever else drifted between them."""
    path = tmp_path / name
    src = (fixtures_dir / "plan_two_stage.toml").read_text(encoding="utf-8")
    path.write_text(src + order_toml, encoding="utf-8")
    return pp._plan_content_digest(plan.load_plan(str(path)))


@pytest.mark.parametrize(
    "edited, why",
    [
        (
            _ORDER.replace(
                "the norm governing an act of activity in this engine",
                "the norm governing an act of activity, its filling defective",
            ),
            "a re-wording: the same requirements, a different statement of the place",
        ),
        (
            _ORDER.replace("R1", "R2"),
            "an id/coverage-key change: what the plan is for is stated under a new key",
        ),
        (
            _ORDER.replace(
                'not one free-text string" },\n',
                'not one free-text string" },\n  "a requirement written as a sentence",\n',
            ),
            "an added requirement the parser DROPS: every readable field is identical, so "
            "`Order.malformed` is the only thing that moves",
        ),
    ],
    ids=["rewording", "id_and_coverage_key", "a_dropped_requirement"],
)
def test_an_order_only_edit_moves_the_content_digest(edited, why, tmp_path, fixtures_dir):
    """A question is raised against the statement of what the plan is FOR, and the order is
    now where that statement lives. So an edit confined to `[meta.order]` has to re-arm
    `_ENUMERATE_STALE`, exactly as a goal or a stage edit does.

    Three edit classes, each failing differently. A re-wording changes only prose the scope
    key deliberately ignores, so a digest built from `order_scope` would miss it; an id or
    coverage-key change is the scope edit a replan actually makes; and a requirement added
    in a shape the parser DROPS leaves every readable field identical, so only
    `Order.malformed` moves — the case that would otherwise let an order be edited with no
    key in the family noticing at all. The digest reads `order_place`, the wider of the two
    keys and the one that carries `malformed`, and catches all three.

    Nothing in the suite caught this before: every corpus plan and every fixture has `order
    is None`, where the contribution is empty whether or not the field is read at all."""
    before = _with_order(tmp_path, fixtures_dir, "before.toml")
    after = _with_order(tmp_path, fixtures_dir, "after.toml", edited)

    assert before != after, f"an order-only edit left the digest unmoved — {why}"


def test_an_order_only_edit_reblocks_a_discharged_enumeration(tmp_path, fixtures_dir):
    """The digest difference above, carried through to the gate it exists to arm: an
    enumeration discharged against the old order does not survive the new one."""
    path = tmp_path / "plan.toml"
    src = (fixtures_dir / "plan_two_stage.toml").read_text(encoding="utf-8")
    path.write_text(src + _ORDER, encoding="utf-8")

    state = _new_state(plan_path=str(path))
    plugins.activate(state, "premise")
    _cover_the_order(state)
    state.plugins["premise"]["enumerated"] = True
    state.plugins["premise"]["enumerated_at"] = pp._plan_content_digest(
        plan.load_plan(str(path))
    )
    assert plugins.plugin_gate_blockers(state, "plan_approval") == []

    # the replan rewrites the order and nothing else — same goal, same stages
    path.write_text(src + _ORDER.replace("R1", "R2"), encoding="utf-8")

    assert plugins.plugin_gate_blockers(state, "plan_approval") == [
        f"[premise] {pp._ENUMERATE_STALE}"
    ]


def test_an_orderless_plan_s_digest_is_unchanged_by_the_order_field(tmp_path, fixtures_dir):
    """The identity that makes the change above safe to ship. `enumerated_at` is PERSISTED
    and compared across processes, so a contribution made unconditionally would re-arm
    `_ENUMERATE_STALE` for every live session the moment this field arrived — a plan
    nobody edited would suddenly need a re-run of `question-enumerate`.

    Pinned as a literal digest rather than as `order_place(...) == ()`: the property is
    about the BYTES this function returns for an order-less plan, and a payload change that
    kept the empty contribution but reshaped the tuple around it would still move them."""
    orderless = pp._plan_content_digest(
        plan.load_plan(str(fixtures_dir / "plan_two_stage.toml"))
    )

    assert orderless == (
        "16d4cb1479155b598093362b1a136cb773c378251f299374dbc8083f429277d3"
    ), (
        "before re-pinning this literal: confirm order_place(meta) is still () for an "
        "order-less plan, and that the order is still spliced onto the payload tuple "
        "rather than occupying a slot in it — re-pinning without checking both silently "
        "converts this test into 'whatever the code currently does'"
    )


# --- enumerate round-release: staleness blocker collapses; others survive -------

def _stale_bag_state(plan_path, *, passes=3):
    """A substantive session with a stale enumeration (enumerated_at mismatches the
    current plan content) and `enumerate_pass` set to `passes`. Question and order
    halves are pre-satisfied so only the staleness branch can fire."""
    state = _new_state(plan_path=plan_path, weight_class=WeightClass.SUBSTANTIVE.value)
    plugins.activate(state, "premise")
    _cover_the_order(state)
    bag = state.plugins["premise"]
    bag["enumerated"] = True
    bag["enumerated_at"] = "a-stale-digest-from-an-earlier-plan"
    bag["enumerate_pass"] = passes
    return state, bag


def test_staleness_still_blocks_below_threshold(fixtures_dir):
    """RED arm: one pass below the threshold the routing message must NOT appear —
    _ENUMERATE_STALE still blocks, byte-identical to before this stage."""
    plan_path = str(fixtures_dir / "plan_two_stage.toml")
    state, _ = _stale_bag_state(plan_path, passes=2)  # threshold is 3
    blockers = plugins.plugin_gate_blockers(state, "plan_approval")
    assert blockers == [f"[premise] {pp._ENUMERATE_STALE}"]


def test_staleness_blocker_collapses_to_routing_message_at_threshold(fixtures_dir):
    """At threshold, the routing message replaces _ENUMERATE_STALE and names the
    typed escape. The gate stays non-empty (never auto-approves)."""
    plan_path = str(fixtures_dir / "plan_two_stage.toml")
    state, _ = _stale_bag_state(plan_path, passes=3)
    blockers = plugins.plugin_gate_blockers(state, "plan_approval")
    assert len(blockers) == 1
    assert pp._ENUMERATE_STALE not in blockers[0]
    assert "enumeration round budget exhausted" in blockers[0]
    assert f"--reason {premise.ESCAPE_ENUMERATE_ROUNDS_EXHAUSTED}" in blockers[0]
    assert "at pass 3" in blockers[0]


def test_staleness_routing_message_carries_live_pass_count(fixtures_dir):
    """The message contains the LIVE count, not a threshold-shaped literal."""
    plan_path = str(fixtures_dir / "plan_two_stage.toml")
    state, _ = _stale_bag_state(plan_path, passes=5)
    blockers = plugins.plugin_gate_blockers(state, "plan_approval")
    assert "at pass 5" in blockers[0]


def test_other_premise_blockers_survive_active_release(fixtures_dir):
    """The release collapses ONLY the staleness blocker — an undispositioned question
    still blocks, mirroring the invariant that the release never approves by itself."""
    plan_path = str(fixtures_dir / "plan_two_stage.toml")
    state, bag = _stale_bag_state(plan_path, passes=3)
    bag["questions"] = [{
        "id": "Q1", "target": "plan.goal", "statement": "what is the goal?",
        "disposition": "open", "reason": "",
    }]
    blockers = plugins.plugin_gate_blockers(state, "plan_approval")
    question_blocker = any("Q1" in b for b in blockers)
    routing_blocker = any("enumeration round budget" in b for b in blockers)
    assert question_blocker, f"expected open-question blocker, got: {blockers}"
    assert routing_blocker, f"expected staleness routing message, got: {blockers}"


def test_staleness_cleared_after_escape_recorded(fixtures_dir, store):
    """Once the user records the typed escape, premise_blockers clears the staleness
    branch. Other blockers still stand — the escape is not a global approve."""
    from argparse import Namespace
    plan_path = str(fixtures_dir / "plan_two_stage.toml")
    state, bag = _stale_bag_state(plan_path, passes=3)
    store.save(state)

    d = cli.cmd_question_enumerate_escape(
        Namespace(session="s", reason=premise.ESCAPE_ENUMERATE_ROUNDS_EXHAUSTED,
                  note="the plan is acceptable at this pass count", plan=None),
        store=store)
    assert d.ok, d.detail

    state2 = store.load("s")
    blockers = plugins.plugin_gate_blockers(state2, "plan_approval")
    assert not any("enumeration round budget" in b for b in blockers), blockers
    assert not any(pp._ENUMERATE_STALE in b for b in blockers), blockers


# --- #60 residual: disclose an in-flight enumeration in the essence coverage block

def _armed_bag(plan_path, **overrides):
    state = _new_state(plan_path=plan_path)
    plugins.activate(state, "premise")
    _cover_the_order(state)
    bag = state.plugins["premise"]
    bag.update(overrides)
    return state, bag


def test_enumeration_in_flight_false_with_no_launch(fixtures_dir):
    plan_path = str(fixtures_dir / "plan_two_stage.toml")
    _, bag = _armed_bag(plan_path)
    assert pp._enumeration_in_flight(bag) is False


def test_enumeration_in_flight_true_while_outstanding(fixtures_dir):
    plan_path = str(fixtures_dir / "plan_two_stage.toml")
    _, bag = _armed_bag(
        plan_path, enumerate_launch=1, enumerated=False,
        enumerate_deadline=time.time() + 100,
    )
    assert pp._enumeration_in_flight(bag) is True


def test_enumeration_in_flight_false_once_landed(fixtures_dir):
    """A launch is on record, but the pass already landed — no window is open."""
    plan_path = str(fixtures_dir / "plan_two_stage.toml")
    _, bag = _armed_bag(
        plan_path, enumerate_launch=1, enumerated=True,
        enumerate_deadline=time.time() + 100,
    )
    assert pp._enumeration_in_flight(bag) is False


def test_enumeration_in_flight_false_past_deadline(fixtures_dir):
    """The deadline elapsed with nothing landed — that is `enumeration_not_landed`'s
    window to name via the escape, not this disclosure line's; claiming "in flight"
    for a launch that has gone missing would misstate what is actually happening."""
    plan_path = str(fixtures_dir / "plan_two_stage.toml")
    _, bag = _armed_bag(
        plan_path, enumerate_launch=1, enumerated=False,
        enumerate_deadline=time.time() - 1,
    )
    assert pp._enumeration_in_flight(bag) is False


def test_enumeration_in_flight_false_with_no_deadline_stamped():
    """A bag minted before enumerate_deadline existed reads as not-in-flight, never
    raises on the None -> float comparison."""
    assert pp._enumeration_in_flight({"enumerate_launch": 1, "enumerated": False,
                                      "enumerate_deadline": None}) is False


def test_coverage_block_omits_the_line_with_no_launch_outstanding(fixtures_dir):
    plan_path = str(fixtures_dir / "plan_two_stage.toml")
    state, bag = _armed_bag(plan_path)
    block = pp.coverage_block(state, bag)
    assert "enumeration in flight" not in block


def test_coverage_block_carries_the_line_while_a_launch_is_outstanding(fixtures_dir):
    plan_path = str(fixtures_dir / "plan_two_stage.toml")
    state, bag = _armed_bag(
        plan_path, enumerate_launch=2, enumerated=False,
        enumerate_deadline=time.time() + 100,
    )
    block = pp.coverage_block(state, bag)
    [line] = [ln for ln in block.splitlines() if "enumeration in flight" in ln]
    assert "launch 2" in line
    assert "question-enumerate" in line


def test_coverage_block_missing_lines_picks_up_the_in_flight_line(fixtures_dir):
    """The same mechanical containment check every other coverage-block line rides:
    an essence presented BEFORE the launch went out no longer carries the line, so
    the gate must name it missing — proving the new line is wired into the existing
    #60 essence-must-carry-the-block mechanism, not a parallel one."""
    plan_path = str(fixtures_dir / "plan_two_stage.toml")
    state, bag = _armed_bag(plan_path)
    rendering = pp.coverage_block(state, bag)  # presented before the launch

    bag["enumerate_launch"] = 1
    bag["enumerate_deadline"] = time.time() + 100
    live_block = pp.coverage_block(state, bag)
    missing = pp.coverage_block_missing_lines(live_block, rendering)
    assert any("enumeration in flight" in m for m in missing)
