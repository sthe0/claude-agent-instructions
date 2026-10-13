#!/usr/bin/env python3
"""Mutation catalogue for the review-exhaustion reflection exit: proves its tests discriminate.

Difficulty removed: a test that no wrong implementation can turn red is decoration. Each
catalogue entry is one wrong version of a production line, applied by an exact textual
substitution to a scratch copy of scripts/ (plus the config.md the thresholds are read
from), together with the test ids that must fail on it.

  python3 scripts/tests/rx_mutation_control.py --mutant NAME   apply one mutant, run its tests
  python3 scripts/tests/rx_mutation_control.py --control        run every listed test, unmutated
  python3 scripts/tests/rx_mutation_control.py --list

Exit codes of --mutant:
  1  killed: the anchor matched exactly once and every listed id was collected once and
     FAILED in its call phase
  0  survived: every listed id was collected once, none errored or skipped, but at least one passed
  3  the anchor did not match exactly once (the production line moved)
  4  collection / import error in the child pytest
  5  a listed id was collected zero or several times, errored, or was skipped
  6  unhandled exception in this script, a nested run (RX_MUTATION_CHILD already set), or a
     catalogue id that is not one plain test function of the review-exhaustion test module
  2  usage (argparse)
Exit codes of --control: 0 all listed ids green on the unmutated copy, 7 otherwise.

The child pytest runs only the listed ids, serially, with RX_MUTATION_CHILD=1 so the in-suite
catalogue test does not recurse.
"""
from __future__ import annotations

import argparse
import os
import re
import shutil
import subprocess
import sys
import tempfile
import xml.etree.ElementTree as ET
from dataclasses import dataclass
from pathlib import Path

SCRIPTS_DIR = Path(__file__).resolve().parent.parent
REPO_ROOT = SCRIPTS_DIR.parent
GATES = "agentctl/gates.py"
CLI = "agentctl/cli.py"
TASK_ACCUMULATOR = "agentctl/task_accumulator.py"
T_RX = "tests/test_review_exhaustion_reflection.py"
CATALOGUE_TEST = "test_rx_mutation_catalogue_kills_every_mutant"
SIBLING_DATA = ("config.md", "docs/operations/review-backstop-arming.json")

KILLED, SURVIVED, ANCHOR_MISS, COLLECT_ERROR, BAD_ID, CRASH = 1, 0, 3, 4, 5, 6
CONTROL_RED = 7

_SELECTOR = re.compile(r"^" + re.escape(T_RX) + r"::[A-Za-z_][A-Za-z0-9_]*$")


@dataclass(frozen=True)
class Mutant:
    relpath: str
    anchor: str
    replacement: str
    ids: "tuple[str, ...]"


def _ids(*names: str) -> "tuple[str, ...]":
    return tuple(f"{T_RX}::{name}" for name in names)


_SINCE_ROUNDS_AXIS = '"review_rounds_since_user_decision"'
_SINCE_REPLANS_AXIS = '"replans_since_user_decision"'
_BACKSTOP_GUARD = "    if spent < backstop:\n        return None\n"
_BOUNDARY_OUTSIDE = (
    '        outside = [] if boundary["eligible"] else '
    'list(boundary.get("reasons") or [boundary["reason"]])\n'
)

CATALOGUE: "dict[str, Mutant]" = {
    "reflection-listed-first": Mutant(
        GATES,
        "    message = message_template.format(rounds=counter.value(state))\n",
        '    message = "go ahead with plan-review --verdict override, or: " '
        "+ message_template.format(rounds=counter.value(state))\n",
        _ids(
            "test_whole_plan_release_message_orders_reflection_then_fresh_pass_then_override",
            "test_post_pass_release_message_orders_reflection_before_override",
            "test_cross_axis_release_message_orders_reflection_before_override",
        ),
    ),
    "override-requires-record": Mutant(
        CLI,
        "        if refused is not None:\n            return (\n"
        '                f"an agent-authored override is refused: {refused}. Run',
        "        if False:\n            return (\n"
        '                f"an agent-authored override is refused: {refused}. Run',
        _ids("test_agent_override_refused_without_any_difficulty_record"),
    ),
    "override-record-fresh": Mutant(
        CLI,
        "    if did == anchor_id:\n",
        "    if False:\n",
        _ids("test_agent_override_refused_for_a_record_declared_before_the_release"),
    ),
    "override-record-consumed": Mutant(
        CLI,
        "    if did in state.agent_ack_difficulty_ids:\n",
        "    if False:\n",
        _ids("test_agent_override_consumes_its_difficulty_record"),
    ),
    "override-needs-normalization": Mutant(
        CLI,
        "    if diff.normalization is None or not diff.normalization.factor.strip():\n",
        "    if False:\n",
        _ids("test_agent_override_needs_a_real_normalization"),
    ),
    "review-round-backstop": Mutant(
        CLI,
        _BACKSTOP_GUARD,
        f"    if spent < backstop or axis == {_SINCE_ROUNDS_AXIS}:\n        return None\n",
        _ids("test_agent_override_refused_at_the_review_round_backstop"),
    ),
    "replan-backstop": Mutant(
        CLI,
        _BACKSTOP_GUARD,
        f"    if spent < backstop or axis == {_SINCE_REPLANS_AXIS}:\n        return None\n",
        _ids(
            "test_agent_fire_acknowledge_refused_at_the_replan_backstop",
            "test_agent_renegotiation_continue_refused_at_the_replan_backstop",
        ),
    ),
    "agent-continue-keeps-backstop": Mutant(
        CLI,
        "    if (by or \"\").strip().casefold() == AGENT_ACTOR:\n        return\n"
        "    task_accumulator.clear_since_user_decision(",
        "    task_accumulator.clear_since_user_decision(",
        _ids(
            "test_agent_fire_acknowledge_leaves_the_since_user_counters",
            "test_agent_renegotiation_continue_leaves_the_since_user_counters",
        ),
    ),
    "user-decision-resets-backstop": Mutant(
        CLI,
        "    task_accumulator.clear_since_user_decision(\n"
        "        state.task_id, session_id=state.session_id, now=_utcnow())\n",
        "    return\n",
        _ids(
            "test_user_approval_zeroes_the_since_user_counters",
            "test_user_fire_acknowledgement_zeroes_the_since_user_counters",
            "test_user_renegotiation_zeroes_the_since_user_counters",
            "test_user_plan_review_override_zeroes_the_since_user_counters",
        ),
    ),
    "backstop-boundary-ignored": Mutant(
        CLI,
        _BOUNDARY_OUTSIDE,
        "        outside = []\n",
        _ids(
            "test_agent_override_refused_at_the_review_round_backstop",
            "test_agent_fire_acknowledge_refused_at_the_replan_backstop",
            "test_agent_renegotiation_continue_refused_at_the_replan_backstop",
            "test_backstop_refusal_outside_the_boundary_names_the_boundary_reasons",
        ),
    ),
    "backstop-boundary-always-outside": Mutant(
        CLI,
        _BOUNDARY_OUTSIDE,
        '        outside = ["forced outside the boundary"]\n',
        _ids(
            "test_agent_override_admitted_past_the_review_round_backstop_inside_the_boundary",
            "test_agent_fire_acknowledge_admitted_past_the_replan_backstop_inside_the_boundary",
            "test_agent_renegotiation_continue_admitted_past_the_replan_backstop_inside_the_boundary",
        ),
    ),
    "backstop-crossing-unrecorded": Mutant(
        CLI,
        "    crossing.update(session=state.session_id, ts=_utcnow())\n",
        "    return\n    crossing.update(session=state.session_id, ts=_utcnow())\n",
        _ids(
            "test_backstop_crossing_is_recorded_once_with_its_axis_count_and_backstop",
            "test_backstop_crossing_is_disclosed_at_verify_final_and_resolve",
            "test_agent_override_admitted_past_the_review_round_backstop_inside_the_boundary",
        ),
    ),
    "backstop-crossing-not-kept-per-task": Mutant(
        CLI,
        "    task_accumulator.record_crossing(\n"
        '        state.task_id, crossing, session_id=state.session_id, now=crossing["ts"])\n',
        "",
        _ids(
            "test_backstop_crossing_is_disclosed_by_a_later_session_of_the_same_task",
            "test_backstop_crossing_is_disclosed_after_the_session_is_reset_and_a_user_decision",
            "test_backstop_crossing_is_disclosed_to_a_session_with_no_history_of_its_own",
        ),
    ),
    "backstop-task-record-not-read": Mutant(
        CLI,
        '    recorded = task_accumulator.get(state.task_id)["agent_backstop_crossings"]\n',
        "    recorded = []\n",
        _ids(
            "test_backstop_crossing_is_disclosed_by_a_later_session_of_the_same_task",
            "test_backstop_crossing_is_disclosed_after_the_session_is_reset_and_a_user_decision",
            "test_backstop_crossing_is_disclosed_to_a_session_with_no_history_of_its_own",
        ),
    ),
    "backstop-crossing-record-reset": Mutant(
        TASK_ACCUMULATOR,
        '        data["agent_backstop_crossings"] = previous["agent_backstop_crossings"]\n',
        "",
        _ids("test_backstop_crossing_record_survives_the_accumulator_reset"),
    ),
    "backstop-crossing-record-user-cleared": Mutant(
        TASK_ACCUMULATOR,
        "        for axis in SINCE_USER_AXES:\n            data[\"per_axis_totals\"][axis] = 0\n",
        "        for axis in SINCE_USER_AXES:\n            data[\"per_axis_totals\"][axis] = 0\n"
        "        data[\"agent_backstop_crossings\"] = []\n",
        _ids("test_backstop_crossing_is_disclosed_after_the_session_is_reset_and_a_user_decision"),
    ),
    "backstop-crossings-not-cleared-at-resolve": Mutant(
        CLI,
        "    task_accumulator.discard_crossings(\n"
        '        state.task_id, data["agent_backstop_crossings"], now=_utcnow())\n',
        "",
        _ids(
            "test_resolve_clears_the_crossings_it_disclosed_so_a_later_session_of_the_task_starts_clean",
            "test_resolve_keeps_a_crossing_recorded_after_the_disclosure_snapshot",
            "test_a_refused_resolve_leaves_the_crossing_record_in_place",
        ),
    ),
    "backstop-crossings-cleared-wholesale-at-resolve": Mutant(
        CLI,
        '        state.task_id, data["agent_backstop_crossings"], now=_utcnow())\n',
        '        state.task_id, task_accumulator.get(state.task_id)["agent_backstop_crossings"],\n'
        "        now=_utcnow())\n",
        _ids("test_resolve_keeps_a_crossing_recorded_after_the_disclosure_snapshot"),
    ),
    "backstop-crossings-cleared-before-the-resolve-gates": Mutant(
        CLI,
        "def cmd_resolve(args, *, store: StateStore, runner: Runner | None = None) -> Directive:\n"
        "    state = _require(store, args.session)\n",
        "def cmd_resolve(args, *, store: StateStore, runner: Runner | None = None) -> Directive:\n"
        "    state = _require(store, args.session)\n"
        "    task_accumulator.discard_crossings(state.task_id, _backstop_crossings(state))\n",
        _ids("test_a_refused_resolve_leaves_the_crossing_record_in_place"),
    ),
    "backstop-crossings-clearing-not-fail-open": Mutant(
        CLI,
        "    except Exception as exc:  # noqa: BLE001 - fail-open by design\n"
        '        data["agent_backstop_crossings_cleared"] = False\n',
        "    except KeyError as exc:  # noqa: BLE001 - fail-open by design\n"
        '        data["agent_backstop_crossings_cleared"] = False\n',
        _ids("test_resolve_survives_a_fault_clearing_the_crossing_record"),
    ),
    "backstop-spend-bound-ignored": Mutant(
        CLI,
        "            for scale, ratio in armed.items() if ratio >= multiple]\n",
        "            for scale, ratio in armed.items() if False]\n",
        _ids("test_backstop_crossings_stop_once_spend_reaches_the_divergence_multiple"),
    ),
    "backstop-no-estimate-admitted": Mutant(
        CLI,
        "        return [_NO_ARMED_ESTIMATE]\n",
        "        return []\n",
        _ids("test_backstop_crossing_refused_when_no_estimate_is_armed"),
    ),
    "backstop-unapproved-session-admitted": Mutant(
        CLI,
        "    if _ledgered_order_key(state) is None:\n        outside.append(_NO_APPROVED_VERSION)\n",
        "    if False:\n        outside.append(_NO_APPROVED_VERSION)\n",
        _ids("test_backstop_crossing_refused_for_a_session_with_no_user_approved_version"),
    ),
    "backstop-renegotiation-crossing-unsaved": Mutant(
        CLI,
        "        # lands with it: a later gate below may still refuse this replan.\n"
        "        store.save(state)\n",
        "        # lands with it: a later gate below may still refuse this replan.\n",
        _ids("test_renegotiation_crossing_is_kept_when_a_later_gate_refuses_the_replan"),
    ),
    "backstop-crossing-skips-later-checks": Mutant(
        CLI,
        '            return f"an agent-authored override is refused: {backstopped}", None\n'
        "        refused = _agent_self_decision_refusal(\n",
        '            return f"an agent-authored override is refused: {backstopped}", None\n'
        "        if crossing is not None:\n            return None, crossing\n"
        "        refused = _agent_self_decision_refusal(\n",
        _ids("test_crossing_inside_the_boundary_still_needs_the_reflection_exit"),
    ),
    "exhaustion-record-not-reused": Mutant(
        CLI,
        "    push = cur is not None and (nested or cur.declaration is not None)\n",
        "    push = cur is not None and (nested or cur.declaration is not None)\n"
        "    if push and not nested and declaration is None:\n        return cur\n",
        _ids("test_exhaustion_time_record_is_not_the_diagnosing_cycles_record"),
    ),
    "since-user-replans-increment": Mutant(
        CLI,
        '        mirror_axis="replans_since_user_decision",\n',
        "",
        _ids("test_a_replan_advances_the_since_user_replan_counter_with_the_task_total"),
    ),
    "since-user-rounds-increment": Mutant(
        CLI,
        '        mirror_axis="review_rounds_since_user_decision", **digest,\n',
        "        **digest,\n",
        _ids("test_counted_thinker_reviews_advance_the_since_user_round_counter"),
    ),
}


def validate_selector(node: str) -> None:
    """A catalogue id must name exactly one plain, non-parametrized test function of the
    review-exhaustion module, and never the catalogue test itself (it would recurse)."""
    if not _SELECTOR.match(node):
        raise ValueError(f"not a single plain test node of {T_RX}: {node!r}")
    if node.endswith("::" + CATALOGUE_TEST):
        raise ValueError(f"the catalogue test cannot be a catalogue id: {node!r}")


def validate_catalogue(catalogue: "dict[str, Mutant]" = CATALOGUE) -> None:
    for name, mutant in catalogue.items():
        if not mutant.ids:
            raise ValueError(f"{name}: no test ids")
        for node in mutant.ids:
            validate_selector(node)


def _junit_status(xml_path: Path) -> "dict[tuple[str, str], list[str]]":
    """(module stem, test name) -> statuses seen: failure | error | skipped | passed."""
    seen: "dict[tuple[str, str], list[str]]" = {}
    for case in ET.parse(xml_path).getroot().iter("testcase"):
        stem = case.get("classname", "").rsplit(".", 1)[-1]
        status = "passed"
        for tag in ("failure", "error", "skipped"):
            if case.find(tag) is not None:
                status = tag
                break
        seen.setdefault((stem, case.get("name", "")), []).append(status)
    return seen


def _run_child(subject: Path, ids: "tuple[str, ...]", xml_path: Path) -> int:
    env = dict(os.environ)
    env.update(PYTHONPATH=str(subject), RX_MUTATION_CHILD="1", PYTHONDONTWRITEBYTECODE="1")
    env.pop("PYTEST_ADDOPTS", None)
    command = [
        sys.executable, "-m", "pytest", "-p", "no:xdist", "-p", "no:cacheprovider", "-q", "--tb=no",
        f"--junitxml={xml_path}", *ids,
    ]
    return subprocess.run(command, cwd=subject, env=env, capture_output=True, text=True).returncode


def _copy_subject(parent: Path) -> Path:
    """scripts/ plus the repo-root files the engine reads its thresholds and the arming
    record from, laid out as in the repo so `REPO_ROOT / "config.md"` resolves."""
    subject = parent / "scripts"
    shutil.copytree(SCRIPTS_DIR, subject, symlinks=True,
                    ignore=shutil.ignore_patterns("__pycache__", ".pytest_cache"))
    for rel in SIBLING_DATA:
        source = REPO_ROOT / rel
        if source.is_file():
            target = parent / rel
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(source, target)
    return subject


def _classify(ids: "tuple[str, ...]", xml_path: Path, child_rc: int) -> "tuple[str, int | None]":
    """Return (description, early exit code or None). None means every id passed or failed cleanly."""
    if child_rc in (2, 3, 4) or not xml_path.exists():
        return f"pytest rc {child_rc}: collection or usage error", COLLECT_ERROR
    seen = _junit_status(xml_path)
    outcomes = []
    for node in ids:
        path, name = node.split("::")
        statuses = seen.get((Path(path).stem, name), [])
        if len(statuses) != 1:
            return f"{node} collected {len(statuses)} times", BAD_ID
        if statuses[0] in ("error", "skipped"):
            return f"{node} {statuses[0]}", BAD_ID
        outcomes.append(statuses[0])
    return ",".join(outcomes), None


def run_mutant(name: str) -> int:
    mutant = CATALOGUE[name]
    try:
        with tempfile.TemporaryDirectory(prefix="rx-mutant-") as tmp:
            subject = _copy_subject(Path(tmp))
            target = subject / mutant.relpath
            source = target.read_text(encoding="utf-8")
            if source.count(mutant.anchor) != 1:
                print(f"{name}: anchor matched {source.count(mutant.anchor)} times in {mutant.relpath}",
                      file=sys.stderr)
                return ANCHOR_MISS
            target.write_text(source.replace(mutant.anchor, mutant.replacement), encoding="utf-8")
            xml_path = Path(tmp) / "result.xml"
            rc = _run_child(subject, mutant.ids, xml_path)
            description, early = _classify(mutant.ids, xml_path, rc)
            if early is not None:
                print(f"{name}: {description}", file=sys.stderr)
                return early
            return KILLED if all(s == "failure" for s in description.split(",")) else SURVIVED
    except Exception as exc:
        print(f"{name}: unhandled {type(exc).__name__}: {exc}", file=sys.stderr)
        return CRASH


def run_control() -> int:
    ids = tuple(dict.fromkeys(i for m in CATALOGUE.values() for i in m.ids))
    with tempfile.TemporaryDirectory(prefix="rx-control-") as tmp:
        subject = _copy_subject(Path(tmp))
        xml_path = Path(tmp) / "result.xml"
        rc = _run_child(subject, ids, xml_path)
        description, early = _classify(ids, xml_path, rc)
        if early is not None:
            print(f"control: {description}", file=sys.stderr)
            return CONTROL_RED
        return 0 if all(s == "passed" for s in description.split(",")) else CONTROL_RED


def main(argv: "list[str] | None" = None) -> int:
    if os.environ.get("RX_MUTATION_CHILD"):
        print("refusing to run inside a mutation child (RX_MUTATION_CHILD is set)", file=sys.stderr)
        return CRASH
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument("--mutant", choices=sorted(CATALOGUE))
    group.add_argument("--control", action="store_true")
    group.add_argument("--list", action="store_true")
    args = parser.parse_args(argv)
    validate_catalogue()
    if args.list:
        for name, mutant in CATALOGUE.items():
            print(f"{name} {mutant.relpath} {len(mutant.ids)} ids")
        return 0
    if args.control:
        return run_control()
    code = run_mutant(args.mutant)
    print(f"{args.mutant}: exit {code} ({'killed' if code == KILLED else 'not killed'})")
    return code


if __name__ == "__main__":
    try:
        sys.exit(main())
    except SystemExit:
        raise
    except BaseException as exc:
        print(f"unhandled {type(exc).__name__}: {exc}", file=sys.stderr)
        sys.exit(CRASH)
