"""Fault-address routing at difficulty closure (R2, reframed v4).

A затруднение is overcome by fixing its обеспечение, and a goal-failure is ambiguous
until ROUTED: was the inadequate обеспечение resource (материал/средство — 'resource')
or normative (норма/способ — 'normative'), or does routing explicitly not apply
(not_applicable)? These are two special cases of ONE act («норма — тоже ресурс»), both
reducing reflexively to знание — NOT an is/ought (сущее/должное) tag; that v3 typing was
rejected (ADR-0004 §R2). The engine types the routing as `Critique.failure_address` and
enforces it as a `replan` precondition at DIAGNOSING closure (gates.failure_address_blockers):
once the declare->investigate->critique cycle is complete, replan is blocked until the
critique carries a routing value. A bare None (omission) blocks — the routing must be
DECIDED; an EXPLICIT not_applicable is a legal opt-out that clears. This mirrors R3's
normalization gate over the renorming act; the SMD content (which обеспечение failed) is
the coordinator's cognition, the routing's EXISTENCE is the engine's.

The closure gate checks ONLY non-None: any recorded value clears, so a legacy record
carrying an OLD сущее/должное value (the rejected v3 typing) is grandfathered, never
re-blocked. Bogus values never reach a persisted record — cmd_critique and the argparse
`choices` reject them at write time. This covers the deterministic SHELL: the pure
guardian, cmd_critique validation, the argparse `choices` guard, round-trip persistence,
the legacy grandfather migration, and the end-to-end block/route path at closure."""
import re
from argparse import Namespace
from pathlib import Path

import pytest

from agentctl import cli, gates
from agentctl.state import (
    canonical_failure_address,
    FAILURE_ADDRESS_ALIASES,
    Critique,
    Declaration,
    Difficulty,
    Investigation,
    Node,
    SessionState,
    StageStatus,
    FAILURE_ADDRESS_VALUES,
)


def ns(**kw):
    return Namespace(**kw)


# FAILURE_ADDRESS_VALUES routes the fault to the inadequate обеспечение (resource vs
# normative) + the not_applicable sentinel — DECOUPLED from the retired StatementKind
# enum (ADR-0004 §R2). Pin the exact set so a future rename trips here.
def test_failure_address_values_are_obespechenie_kinds():
    assert FAILURE_ADDRESS_VALUES == ("resource", "normative", "not_applicable")


# --- guardian unit -----------------------------------------------------------

def _diagnosing(difficulty):
    s = SessionState(session_id="fa", task_id="t", node=Node.DIAGNOSING.value)
    s.difficulty = difficulty
    return s


def _full_difficulty(*, failure_address=None):
    return Difficulty(
        declaration=Declaration("e", "a", "m"),
        investigation=Investigation("le", "la", hypotheses=["h1", "h2"]),
        critique=Critique("fg", "rt", failure_address=failure_address),
    )


def test_blockers_empty_outside_diagnosing():
    s = SessionState(session_id="x", task_id="t", node=Node.VERIFYING.value)
    s.difficulty = _full_difficulty()  # bare None routing, but not at closure
    assert gates.failure_address_blockers(s) == []


def test_blockers_empty_while_cycle_incomplete():
    # difficulty_blockers owns the incomplete-cycle case — this gate never double-reports
    s = _diagnosing(Difficulty(declaration=Declaration("e", "a", "m")))
    assert gates.failure_address_blockers(s) == []


def test_blockers_block_complete_cycle_without_routing():
    s = _diagnosing(_full_difficulty(failure_address=None))
    blockers = gates.failure_address_blockers(s)
    assert blockers and "routing the fault" in blockers[0]


def test_blockers_clear_with_resource_obespechenie():
    s = _diagnosing(_full_difficulty(failure_address="resource"))
    assert gates.failure_address_blockers(s) == []


def test_blockers_clear_with_normative_obespechenie():
    s = _diagnosing(_full_difficulty(failure_address="normative"))
    assert gates.failure_address_blockers(s) == []


def test_blockers_clear_with_explicit_not_applicable():
    # the premise that distinguishes this gate from a mere field-presence check: an
    # EXPLICIT not_applicable is a legal opt-out that clears, distinct from a bare None
    s = _diagnosing(_full_difficulty(failure_address="not_applicable"))
    assert gates.failure_address_blockers(s) == []


def test_blockers_grandfather_legacy_value_at_closure():
    # a legacy record carrying an OLD сущее/должное value (the rejected v3 typing) must
    # still clear — the gate checks ONLY non-None, never membership, so it is never
    # re-blocked at closure. Bogus rejection lives at write time (cmd_critique/argparse).
    for legacy in ("сущее", "должное"):
        s = _diagnosing(_full_difficulty(failure_address=legacy))
        assert gates.failure_address_blockers(s) == []


# --- cmd_critique validation -------------------------------------------------

def _to_failed_stage1(store, sid, plan):
    cli.cmd_start(ns(session=sid, task="fa-demo", goal="", done_criterion="",
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
    return cli.cmd_record_result(ns(session=sid, status="failed", actual="boom"), store=store)


def _declare_investigate(store, sid):
    cli.cmd_declare(ns(session=sid, expected="e", actual="a", mismatch="m"), store=store)
    cli.cmd_investigate(ns(session=sid, localized_expectation="le", localized_actual="la",
                           hypotheses=["h1", "h2"]), store=store)


def test_cmd_critique_accepts_valid_routing(store, venued_fixtures_dir):
    _to_failed_stage1(store, "cc1", str(venued_fixtures_dir / "plan_two_stage.toml"))
    _declare_investigate(store, "cc1")
    d = cli.cmd_critique(ns(session="cc1", functional_ground="fg", replanning_task="rt",
                            failure_address="resource"), store=store)
    assert d.ok is True
    assert store.load("cc1").difficulty.critique.failure_address == "resource"


def test_cmd_critique_rejects_bogus_routing(store, venued_fixtures_dir):
    _to_failed_stage1(store, "cc2", str(venued_fixtures_dir / "plan_two_stage.toml"))
    _declare_investigate(store, "cc2")
    d = cli.cmd_critique(ns(session="cc2", functional_ground="fg", replanning_task="rt",
                            failure_address="maybe"), store=store)
    assert d.ok is False and "failure-address" in d.detail
    # the critique was not written with the bogus value
    assert store.load("cc2").difficulty.critique is None


def test_cmd_critique_omitted_routing_is_none(store, venued_fixtures_dir):
    # omission is legal at critique time (routing may be decided later); the gate is
    # what demands it be present at closure
    _to_failed_stage1(store, "cc3", str(venued_fixtures_dir / "plan_two_stage.toml"))
    _declare_investigate(store, "cc3")
    d = cli.cmd_critique(ns(session="cc3", functional_ground="fg", replanning_task="rt"),
                         store=store)
    assert d.ok is True
    assert store.load("cc3").difficulty.critique.failure_address is None


# --- argparse choices guard (the outermost, declarative layer) ---------------

def test_argparse_rejects_bogus_choice():
    parser = cli.build_parser()
    with pytest.raises(SystemExit):
        parser.parse_args(["critique", "--session", "s", "--functional-ground", "fg",
                           "--replanning-task", "rt", "--failure-address", "maybe"])


def test_argparse_accepts_each_legal_choice():
    parser = cli.build_parser()
    for value in FAILURE_ADDRESS_VALUES:
        args = parser.parse_args(["critique", "--session", "s", "--functional-ground", "fg",
                                  "--replanning-task", "rt", "--failure-address", value])
        assert args.failure_address == value


# --- persistence: round-trip + legacy grandfather migration ------------------

def test_failure_address_round_trips():
    for value in FAILURE_ADDRESS_VALUES:
        s = SessionState(
            session_id="rt", task_id="t", node=Node.DIAGNOSING.value,
            difficulty=_full_difficulty(failure_address=value),
        )
        restored = SessionState.from_json(s.to_json())
        assert restored == s
        assert restored.difficulty.critique.failure_address == value


def test_legacy_critique_without_failure_address_migrates_to_none():
    """A persisted state whose critique omits the failure_address key (pre-R2 JSON) loads
    with failure_address defaulting to None."""
    s = SessionState(
        session_id="mig", task_id="t", node=Node.DIAGNOSING.value,
        difficulty=_full_difficulty(failure_address="normative"),
    )
    data = s.to_dict()
    data["difficulty"]["critique"].pop("failure_address")
    restored = SessionState.from_dict(data)
    assert restored.difficulty.critique.failure_address is None
    # and such a legacy session, at closure, is (correctly) blocked by the gate
    assert gates.failure_address_blockers(restored)


def test_legacy_critique_with_old_value_loads_and_is_ungated():
    """A persisted state whose critique carries an OLD сущее/должное value (the rejected
    v3 typing) still LOADS unchanged and is NOT re-blocked at closure — grandfather."""
    s = SessionState(
        session_id="leg", task_id="t", node=Node.DIAGNOSING.value,
        difficulty=_full_difficulty(failure_address="сущее"),
    )
    restored = SessionState.from_json(s.to_json())
    assert restored.difficulty.critique.failure_address == "сущее"
    assert gates.failure_address_blockers(restored) == []


# --- end-to-end: block / route at closure ------------------------------------

def _renorm(store, sid):
    # clear the normalization gate (checked before failure_address at closure) so the
    # replan reaches — and is decided by — the routing gate
    cli.cmd_normalize(ns(session=sid, factor="reproducible cause", level="note"), store=store)


def test_replan_blocked_without_routing(store, venued_fixtures_dir):
    plan = str(venued_fixtures_dir / "plan_two_stage.toml")
    refined = str(venued_fixtures_dir / "plan_two_stage_refined.toml")
    _to_failed_stage1(store, "e1", plan)
    _declare_investigate(store, "e1")
    cli.cmd_critique(ns(session="e1", functional_ground="fg", replanning_task="rt"),
                     store=store)
    _renorm(store, "e1")
    d = cli.cmd_replan(ns(session="e1", plan=refined), store=store)
    assert d.ok is False
    assert d.action == "critique"
    assert d.data["blockers"]
    # the difficulty is NOT closed — still in DIAGNOSING with the record intact
    assert store.load("e1").node == Node.DIAGNOSING.value


def test_routed_critique_unblocks_replan(store, venued_fixtures_dir):
    plan = str(venued_fixtures_dir / "plan_two_stage.toml")
    refined = str(venued_fixtures_dir / "plan_two_stage_refined.toml")
    _to_failed_stage1(store, "e2", plan)
    _declare_investigate(store, "e2")
    cli.cmd_critique(ns(session="e2", functional_ground="fg", replanning_task="rt",
                        failure_address="normative", norm_targets=["stage:1"]),
                     store=store)
    _renorm(store, "e2")
    d = cli.cmd_replan(ns(session="e2", plan=refined), store=store)
    assert d.ok is True
    state = store.load("e2")
    assert state.node == Node.VERIFYING.value
    assert state.difficulty is None  # cleared on exit
    assert state.stage(1).outcome.status == StageStatus.PENDING.value


def test_explicit_not_applicable_unblocks_replan(store, venued_fixtures_dir):
    # not_applicable is a first-class routing decision, not an omission — it must clear
    plan = str(venued_fixtures_dir / "plan_two_stage.toml")
    refined = str(venued_fixtures_dir / "plan_two_stage_refined.toml")
    _to_failed_stage1(store, "e3", plan)
    _declare_investigate(store, "e3")
    cli.cmd_critique(ns(session="e3", functional_ground="fg", replanning_task="rt",
                        failure_address="not_applicable"), store=store)
    _renorm(store, "e3")
    d = cli.cmd_replan(ns(session="e3", plan=refined), store=store)
    assert d.ok is True
    assert store.load("e3").node == Node.VERIFYING.value


# --- Cyrillic aliases ----------------------------------------------------------

def _critique_args(address):
    return ["critique", "--session", "s", "--functional-ground", "fg", "--replanning-task", "rt",
            "--failure-address", address]


def test_alias_every_routed_value_has_a_cyrillic_spelling():
    assert set(FAILURE_ADDRESS_ALIASES.values()) == set(FAILURE_ADDRESS_VALUES) - {"not_applicable"}
    assert all(alias not in FAILURE_ADDRESS_VALUES for alias in FAILURE_ADDRESS_ALIASES)


@pytest.mark.parametrize("alias", list(FAILURE_ADDRESS_ALIASES))
def test_alias_is_accepted_by_the_cli_parser(alias):
    args = cli.build_parser().parse_args(_critique_args(alias))
    assert args.failure_address == alias


@pytest.mark.parametrize("alias", list(FAILURE_ADDRESS_ALIASES))
def test_alias_cli_critique_stores_the_english_value(store, venued_fixtures_dir, alias):
    _to_failed_stage1(store, "al1", str(venued_fixtures_dir / "plan_two_stage.toml"))
    _declare_investigate(store, "al1")
    english = FAILURE_ADDRESS_ALIASES[alias]
    d = cli.cmd_critique(ns(session="al1", functional_ground="fg", replanning_task="rt",
                            failure_address=alias,
                            norm_targets=["stage:1"] if english == "normative" else []),
                         store=store)
    assert d.ok is True, d.detail
    assert store.load("al1").difficulty.critique.failure_address == english


def test_alias_hard_coded_cyrillic_literal_is_stored_as_english(store, venued_fixtures_dir):
    _to_failed_stage1(store, "al2", str(venued_fixtures_dir / "plan_two_stage.toml"))
    _declare_investigate(store, "al2")
    d = cli.cmd_critique(ns(session="al2", functional_ground="fg", replanning_task="rt",
                            failure_address="ресурсное"), store=store)
    assert d.ok is True
    assert store.load("al2").difficulty.critique.failure_address == "resource"


@pytest.mark.parametrize("alias", list(FAILURE_ADDRESS_ALIASES))
def test_alias_stored_critique_loads_as_english(alias):
    stored = {"critique": {"functional_ground": "fg", "replanning_task": "rt",
                           "failure_address": alias}}
    loaded = Difficulty.from_dict(stored)
    assert loaded.critique.failure_address == FAILURE_ADDRESS_ALIASES[alias]


def test_alias_unknown_stored_value_is_left_unchanged():
    assert canonical_failure_address("сущее") == "сущее"
    assert canonical_failure_address(None) is None


_CYRILLIC_VALUES = re.compile("ресурсное|нормативное|РЕСУРСНОЕ|НОРМАТИВНОЕ")
_REPO = Path(__file__).resolve().parents[2]


def _lines_with_spelling(path):
    return [i for i, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1)
            if _CYRILLIC_VALUES.search(line) and "FAILURE_ADDRESS_ALIASES" not in line]


def test_cyrillic_spellings_stay_on_alias_lines_in_engine_and_docs():
    scanned = [p for p in (_REPO / "scripts" / "agentctl").rglob("*") if p.suffix in {".py", ".md"}]
    scanned.append(_REPO / "scripts" / "spawn-specialist.py")
    offenders = {str(p.relative_to(_REPO)): _lines_with_spelling(p) for p in scanned}
    assert {k: v for k, v in offenders.items() if v} == {}


def test_cyrillic_spellings_in_tests_live_only_in_this_file():
    tests = _REPO / "scripts" / "tests"
    holders = {p.name for p in tests.rglob("*") if p.suffix in {".py", ".toml"}
               and _CYRILLIC_VALUES.search(p.read_text(encoding="utf-8"))}
    assert holders <= {Path(__file__).name}


def test_scan_flags_a_cyrillic_spelling_off_the_alias_line(tmp_path):
    probe = tmp_path / "probe.py"
    probe.write_text('X = "нормативное"\nFAILURE_ADDRESS_ALIASES = {"ресурсное": "resource"}\n',
                     encoding="utf-8")
    assert _lines_with_spelling(probe) == [1]
