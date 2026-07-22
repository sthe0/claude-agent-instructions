#!/usr/bin/env python3
"""Verify the agentctl engine's structural invariants (verify-all check).

This is the static guard for the coordination engine itself — the analogue of
verify-cross-refs.py for prose. It does NOT run the LLM or spawn anything; it
asserts that the deterministic skeleton the engine relies on is internally
consistent:

  1. State schema parses — a minimal SessionState constructs and round-trips
     through the JSON the store (de)serializes (from_json(to_json(s)) == s).
  2. Transition table is sound — every Node is reachable from the start node
     (BLOCKED via the block side-channel, which is intentional and not in the
     pure table) and no non-terminal Node is a dead end with no outgoing edge.
  3. Every cognitive leaf the engine can emit (spawn kinds + the difficulty
     skill) resolves to an existing <config-root>/skills/<name>/SKILL.md
     (config-root resolved via scripts/lib/config_root.py, not hardcoded).
  4. Every engine gate has a guardian hook wired in install-reminder-hooks.sh
     DESIRED (plan_approval -> hook-state-gate.py; resolution ->
     hook-turn-end-gate.py, the end-of-turn structural gate — the
     hook-resolution-reminder.py UserPromptSubmit advisory is retained but is no
     longer the gate's guardian).
  5. The required built-in plugins (dummy, tracker) register at import, extend
     only existing core gates (the scope fence — no new gate), and observe only
     events the engine actually emits.

The reachability / dead-end / gate checks are pure functions taking the table
and node set as arguments so tests can feed a deliberately broken table and
confirm the check fails.

Accepts (and ignores) --staged so verify-all can pass it uniformly. Exit 1 on
any problem.
"""
from __future__ import annotations

import argparse
import os
import re
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
SCRIPTS_DIR = REPO_ROOT / "scripts"
INSTALL_SCRIPT = SCRIPTS_DIR / "install-reminder-hooks.sh"

if str(SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPTS_DIR))

from lib.config_root import skills_dir  # noqa: E402  (needs SCRIPTS_DIR on sys.path)

SKILLS_ROOT = skills_dir()  # isolated system root, not hardcoded ~/.claude

# Skill / specialist names the engine can emit: spawn kinds dispatched via
# spawn-specialist.py (`spawn:<kind>` stage executors) plus the difficulty skill
# routed to on a failed stage. Kept explicit because the engine names them in
# prose Directive detail, not yet a structured registry.
COGNITIVE_LEAVES = ["planner", "developer", "overcome-difficulty"]

# Each engine gate (gates.GUARDIANS key) must have a non-skippable guardian hook
# wired in DESIRED.
GATE_TO_HOOK = {
    "plan_approval": "hook-state-gate.py",
    # The resolution gate's structural guardian is the end-of-turn Stop gate, which
    # can BLOCK a turn that reached all-stages-passed without seeking closure. The
    # hook-resolution-reminder.py UserPromptSubmit advisory is retained as a
    # complementary nudge but is no longer the gate guardian.
    "resolution": "hook-turn-end-gate.py",
}

# Nodes allowed to have no outgoing table edge.
TERMINAL_NODES = {"RESOLVED", "BLOCKED"}
# Nodes reachable by a side-channel command rather than the pure table.
SIDE_CHANNEL_REACHABLE = {"BLOCKED"}


def check_state_roundtrip() -> list[str]:
    from agentctl.state import (
        Actor,
        Critique,
        Criterion,
        Declaration,
        Difficulty,
        Investigation,
        Partition,
        GateRecord,
        Means,
        Node,
        Outcome,
        SessionState,
        Stage,
        StageStatus,
        Subject,
    )

    problems: list[str] = []
    s = SessionState(session_id="vfy", task_id="vfy-task", goal="g")
    if s.node != Node.CLASSIFIED.value:
        problems.append(f"fresh SessionState should start at CLASSIFIED, got {s.node}")
    rt = SessionState.from_json(s.to_json())
    if rt != s:
        problems.append("from_json(to_json(s)) != s for a minimal SessionState")

    # round-trip a richer state (gates + a stage) — exercises the field-wise rebuild
    s2 = SessionState(
        session_id="vfy2",
        task_id="vfy2-task",
        node=Node.EXECUTING.value,
        weight_class="SUBSTANTIVE",
        route="SPAWN",
        approval=GateRecord("plan_approval", armed=True, passed=True, by="user"),
        partition=Partition(m1=True, m2=True, verdict="recommended"),
        stages=[
            Stage(
                index=1,
                title="t",
                subject=Subject(material="m", result="img"),
                means=Means(means="Edit", method="do"),
                actor=Actor(executor="spawn:developer"),
                criterion=Criterion(criterion_type="measurable", done_criterion="dc"),
                outcome=Outcome(status=StageStatus.ACTIVE.value),
            )
        ],
        current_stage=1,
    )
    if SessionState.from_json(s2.to_json()) != s2:
        problems.append("from_json(to_json(s2)) != s2 for a populated SessionState")

    # round-trip a DIAGNOSING state carrying a complete Difficulty record — the
    # overcome-difficulty sub-spine's serialization seam
    s3 = SessionState(
        session_id="vfy3",
        task_id="vfy3-task",
        node=Node.DIAGNOSING.value,
        difficulty=Difficulty(
            declaration=Declaration(expected="e", actual="a", mismatch="m"),
            investigation=Investigation(localized_expectation="le", localized_actual="la"),
            critique=Critique(functional_ground="fg", replanning_task="rt"),
        ),
    )
    rt3 = SessionState.from_json(s3.to_json())
    if rt3 != s3:
        problems.append("from_json(to_json(s3)) != s3 for a DIAGNOSING state with a Difficulty")
    if not (rt3.difficulty and rt3.difficulty.complete()):
        problems.append("round-tripped Difficulty did not report complete()")
    return problems


def reachable_nodes(transitions: dict, start: str) -> set[str]:
    """Forward-reachable node set from `start` over the (event -> (from, to)) table."""
    edges: dict[str, list[str]] = {}
    for frm, to in transitions.values():
        edges.setdefault(frm, []).append(to)
    seen = {start}
    stack = [start]
    while stack:
        node = stack.pop()
        for nxt in edges.get(node, []):
            if nxt not in seen:
                seen.add(nxt)
                stack.append(nxt)
    return seen


def check_reachability(
    all_nodes: set[str], transitions: dict, start: str, side_channel: set[str]
) -> list[str]:
    reached = reachable_nodes(transitions, start) | set(side_channel)
    unreachable = sorted(all_nodes - reached)
    if unreachable:
        return [f"unreachable node(s) from {start}: {unreachable}"]
    return []


def check_dead_ends(all_nodes: set[str], transitions: dict, terminal: set[str]) -> list[str]:
    has_out = {frm for frm, _ in transitions.values()}
    dead = sorted(n for n in all_nodes if n not in terminal and n not in has_out)
    if dead:
        return [f"non-terminal dead-end node(s) with no outgoing edge: {dead}"]
    return []


def check_cognitive_leaves(names: list[str], skills_root: Path) -> list[str]:
    problems: list[str] = []
    for name in names:
        if not (skills_root / name / "SKILL.md").is_file():
            problems.append(
                f"cognitive leaf {name!r} has no SKILL.md at {skills_root / name / 'SKILL.md'}"
            )
    return problems


def parse_desired_hooks(install_script_text: str) -> set[str]:
    """Hook basenames listed in the DESIRED block of install-reminder-hooks.sh."""
    hooks: set[str] = set()
    in_desired = False
    for line in install_script_text.splitlines():
        if re.match(r"\s*DESIRED\s*=\s*\[", line):
            in_desired = True
            continue
        if in_desired:
            if line.strip().startswith("]"):
                break
            for m in re.finditer(r'"([^"]*?\.py)(?:\s[^"]*)?"', line):
                hooks.add(Path(m.group(1)).name)
    return hooks


def check_gate_guardians(gate_to_hook: dict, desired_hooks: set[str]) -> list[str]:
    problems: list[str] = []
    for gate, hook in gate_to_hook.items():
        if hook not in desired_hooks:
            problems.append(f"gate {gate!r} has no guardian hook {hook!r} wired in DESIRED")
    return problems


# Plugins that must be registered at import (importing the plugins module pulls
# every built-in consumer into REGISTRY). Each entry: (name, gate-it-must-extend).
# None == the plugin contributes no gate. A new built-in plugin without an entry
# here is fine; this only pins the ones the engine documents.
REQUIRED_PLUGINS = {
    "dummy": "resolution", "tracker": "resolution", "experience": "resolution",
    "ledger": "resolution", "premise": "plan_approval", "review_dispatch": None,
    "obligations": "resolution",
}
# Plugin observer events must be a subset of the events the engine can emit, or a
# plugin would silently never fire. The event vocabulary is EVENT_FOR_COMMAND.
def check_plugins() -> list[str]:
    from agentctl import gates, plugins

    problems: list[str] = []
    valid_events = set(plugins.EVENT_FOR_COMMAND.values())
    core_gates = set(gates.GUARDIANS)
    for name, gate in REQUIRED_PLUGINS.items():
        plugin = plugins.REGISTRY.get(name)
        if plugin is None:
            problems.append(f"required plugin {name!r} not registered at import (REGISTRY)")
            continue
        if gate is not None and gate not in plugin.gates:
            problems.append(f"plugin {name!r} must extend the {gate!r} gate but does not")
        bad_events = sorted(set(plugin.observers) - valid_events)
        if bad_events:
            problems.append(
                f"plugin {name!r} observes event(s) the engine never emits: {bad_events}"
            )
        # the scope fence: a plugin extends only existing core gates, never a new one
        unknown_gates = sorted(set(plugin.gates) - core_gates)
        if unknown_gates:
            problems.append(
                f"plugin {name!r} keys gate(s) that are not core gates: {unknown_gates}"
            )
    return problems


def check_control_precondition() -> list[str]:
    """Verify the record-result --control precondition for spawn:developer stages.

    A spawn:developer stage must refuse record-result --status passed unless a
    non-empty --control attestation has been supplied. Non-developer stages and
    failed records are always allowed through. No new command must exist for this
    feature — the precondition rides the general record-result command.

    Scoped to this one precondition: the orthogonal code-review gate (also keyed
    on spawn:developer + SUBSTANTIVE) is force-disabled for the duration, the same
    accommodation scripts/tests/conftest.py makes for the pytest suite at large.
    """
    from argparse import Namespace
    from agentctl import cli
    from agentctl.state import (
        Actor, Criterion, GateRecord, Means, Node, Outcome, Partition,
        SessionState, Stage, StageStatus, Subject,
    )

    problems: list[str] = []
    prior_code_review_env = os.environ.get("AGENTCTL_CODE_REVIEW")
    os.environ["AGENTCTL_CODE_REVIEW"] = "0"

    def _dev_stage(index=1, executor="spawn:developer") -> Stage:
        return Stage(
            index=index, title="dev stage",
            subject=Subject(material="m", result="img"),
            means=Means(means="Edit", method="do"),
            actor=Actor(executor=executor),
            criterion=Criterion(criterion_type="measurable", done_criterion="dc"),
            outcome=Outcome(status=StageStatus.ACTIVE.value),
        )

    def _executing_state(stage: Stage) -> SessionState:
        return SessionState(
            session_id="ctrl-check", task_id="ctrl-task",
            node=Node.EXECUTING.value,
            weight_class="SUBSTANTIVE",
            route="SPAWN",
            approval=GateRecord("plan_approval", armed=True, passed=True, by="user"),
            partition=Partition(verdict="not-recommended"),
            stages=[stage],
            current_stage=stage.index,
        )

    class _Mem:
        def __init__(self, s): self.s = s
        def load(self, _): return self.s
        def save(self, s): self.s = s

    # 1. spawn:developer + passed + no --control -> REFUSED
    store = _Mem(_executing_state(_dev_stage()))
    d = cli.cmd_record_result(
        Namespace(session="ctrl-check", status="passed", actual="done", control=None),
        store=store,
    )
    if d.ok:
        problems.append(
            "record-result --status passed on a spawn:developer stage without --control "
            "was not refused (expected ok=False)"
        )
    if "record-result" not in d.detail or "--control" not in d.detail:
        problems.append(
            f"refusal directive does not name 'record-result --control' in its detail: {d.detail!r}"
        )

    # 2. spawn:developer + passed + --control -> ALLOWED (transitions to VERIFYING)
    store2 = _Mem(_executing_state(_dev_stage()))
    d2 = cli.cmd_record_result(
        Namespace(session="ctrl-check", status="passed", actual="done",
                  control="reviewed: self-review ok"),
        store=store2,
    )
    if not d2.ok:
        problems.append(
            f"record-result --status passed with --control was refused unexpectedly: {d2.detail}"
        )
    if d2.ok and store2.s.node != Node.VERIFYING.value:
        problems.append(
            f"after --control accepted, node should be VERIFYING, got {store2.s.node}"
        )

    # 3. spawn:developer + failed + no --control -> enters DIAGNOSING (not a control refusal)
    store3 = _Mem(_executing_state(_dev_stage()))
    d3 = cli.cmd_record_result(
        Namespace(session="ctrl-check", status="failed", actual="boom", control=None),
        store=store3,
    )
    if d3.action == "attest_control":
        problems.append(
            "record-result --status failed on a spawn:developer stage was refused "
            "by the control precondition (control must only block passed, not failed)"
        )

    # 4. in_thread + passed + no --control -> ALLOWED
    store4 = _Mem(_executing_state(_dev_stage(executor="in_thread")))
    d4 = cli.cmd_record_result(
        Namespace(session="ctrl-check", status="passed", actual="done", control=None),
        store=store4,
    )
    if not d4.ok:
        problems.append(
            f"record-result --status passed on an in_thread stage without --control "
            f"was refused unexpectedly (control not required for in_thread): {d4.detail}"
        )

    # 5. no new command carved out for the control feature
    forbidden = {"attest-control", "record-review", "review-result"}
    found = sorted(forbidden & set(cli.COMMANDS))
    if found:
        problems.append(
            f"forbidden new command(s) in COMMANDS (control must ride record-result, "
            f"not a new verb): {found}"
        )

    if prior_code_review_env is None:
        os.environ.pop("AGENTCTL_CODE_REVIEW", None)
    else:
        os.environ["AGENTCTL_CODE_REVIEW"] = prior_code_review_env
    return problems


def check_review_dispatch() -> list[str]:
    """Verify the review_dispatch plugin's two proactive triggers on live state.

    Slot 1 (plan_review -> thinker): firing `submit_plan` on a SUBSTANTIVE session
    at PLAN_READY with an unbound plan review must append exactly one blocking
    directive naming the thinker spawn; once a bound passing PlanReview is
    recorded, the same fire is silent.

    Slot 2 (code_review -> code-reviewer): firing `dispatch` on a SUBSTANTIVE
    session whose active stage is a needs_control() (spawn:developer) stage with
    no bound CodeReview must append exactly one blocking directive naming the
    code-reviewer spawn; once a bound passing CodeReview is recorded, the same
    fire is silent.

    Both fired directly via plugins.fire against an in-memory state, mirroring
    check_control_precondition's style — the real cli.main() -> _fire_plugins
    wiring for the submit_plan/thinker path is exercised end-to-end by
    scripts/tests/test_plugins_review_dispatch.py.
    """
    from agentctl import plugins
    from agentctl.directive import Directive
    from agentctl.state import (
        Actor, CodeReview, Criterion, GateRecord, Means, Node, Outcome,
        Partition, PlanReview, SessionState, Stage, StageStatus, Subject,
    )

    problems: list[str] = []
    prior_pr = os.environ.get("AGENTCTL_PLAN_REVIEW")
    prior_cr = os.environ.get("AGENTCTL_CODE_REVIEW")
    prior_rd = os.environ.get("AGENTCTL_REVIEW_DISPATCH")
    os.environ.pop("AGENTCTL_PLAN_REVIEW", None)
    os.environ.pop("AGENTCTL_CODE_REVIEW", None)
    os.environ.pop("AGENTCTL_REVIEW_DISPATCH", None)
    try:
        plugin = plugins.REGISTRY.get("review_dispatch")
        if plugin is None:
            problems.append("review_dispatch plugin not registered at import (REGISTRY)")
            return problems
        if set(plugin.observers) != {"submit_plan", "dispatch"}:
            problems.append(
                f"review_dispatch observers should be {{'submit_plan', 'dispatch'}}, "
                f"got {set(plugin.observers)}"
            )
        if "replan" in plugin.observers:
            problems.append(
                "review_dispatch must not observe 'replan' (staleness risk — "
                "cmd_replan does not save on a rejected replan)"
            )
        if plugin.gates:
            problems.append(f"review_dispatch must contribute no gate, got {plugin.gates}")

        # --- slot 1: plan_review -> thinker, at PLAN_READY / submit_plan ---
        plan_state = SessionState(
            session_id="rvd-plan", task_id="rvd-task", node=Node.PLAN_READY.value,
            weight_class="SUBSTANTIVE", plan_path="/tmp/verify-agentctl-review-dispatch-plan.toml",
        )
        plugins.activate(plan_state, "review_dispatch")
        fired = plugins.fire("submit_plan", plan_state, Directive(True, plan_state.node, "noop"))
        matches = [f for f in fired if f["plugin"] == "review_dispatch" and f["action"] == "spawn_thinker_review"]
        if len(matches) != 1:
            problems.append(
                f"submit_plan on an unreviewed SUBSTANTIVE plan should fire exactly one "
                f"thinker directive, got {len(matches)}"
            )
        elif matches[0]["data"].get("specialist") != "thinker":
            problems.append(f"thinker directive names the wrong specialist: {matches[0]['data']}")

        # A pass binds only when reviewer-attested (non-empty plan_sha256 matching the
        # live bytes), so write the fixture plan and attest its real digest.
        import hashlib
        Path(plan_state.plan_path).write_text("index = 1\n")
        plan_state.plan_review = PlanReview(
            plan_path=plan_state.plan_path, verdict="pass", reviewer="thinker",
            plan_sha256=hashlib.sha256(Path(plan_state.plan_path).read_bytes()).hexdigest(),
        )
        fired_again = plugins.fire("submit_plan", plan_state, Directive(True, plan_state.node, "noop"))
        Path(plan_state.plan_path).unlink(missing_ok=True)
        if any(f["plugin"] == "review_dispatch" for f in fired_again):
            problems.append("submit_plan directive still fires after a bound passing PlanReview is recorded")

        # --- slot 2: code_review -> code-reviewer, at dispatch ---
        dev_stage = Stage(
            index=1, title="dev stage",
            subject=Subject(material="m", result="img"),
            means=Means(means="Edit", method="do"),
            actor=Actor(executor="spawn:developer"),
            criterion=Criterion(criterion_type="measurable", done_criterion="dc"),
            outcome=Outcome(status=StageStatus.ACTIVE.value),
        )
        dispatch_state = SessionState(
            session_id="rvd-dispatch", task_id="rvd-task", node=Node.EXECUTING.value,
            weight_class="SUBSTANTIVE",
            approval=GateRecord("plan_approval", armed=True, passed=True, by="user"),
            partition=Partition(verdict="not-recommended"),
            stages=[dev_stage], current_stage=1,
        )
        plugins.activate(dispatch_state, "review_dispatch")
        fired3 = plugins.fire("dispatch", dispatch_state, Directive(True, dispatch_state.node, "noop"))
        matches3 = [f for f in fired3 if f["plugin"] == "review_dispatch" and f["action"] == "spawn_code_review"]
        if len(matches3) != 1:
            problems.append(
                f"dispatch on a needs_control() stage with no CodeReview should fire exactly "
                f"one code-reviewer directive, got {len(matches3)}"
            )
        elif matches3[0]["data"].get("specialist") != "code-reviewer":
            problems.append(f"code-review directive names the wrong specialist: {matches3[0]['data']}")

        dispatch_state.code_reviews.append(CodeReview(stage_index=1, verdict="pass", reviewer="code-reviewer"))
        fired4 = plugins.fire("dispatch", dispatch_state, Directive(True, dispatch_state.node, "noop"))
        if any(f["plugin"] == "review_dispatch" for f in fired4):
            problems.append("dispatch directive still fires after a bound passing CodeReview is recorded")
    finally:
        for key, prior in (
            ("AGENTCTL_PLAN_REVIEW", prior_pr),
            ("AGENTCTL_CODE_REVIEW", prior_cr),
            ("AGENTCTL_REVIEW_DISPATCH", prior_rd),
        ):
            if prior is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = prior
    return problems


def check_code_review_precondition() -> list[str]:
    """Verify the record-result code-review gate for spawn:developer stages.

    A SUBSTANTIVE spawn:developer stage that already clears the free-text
    needs_control() floor (check_control_precondition's axis) must still refuse
    record-result --status passed until a bound passing (or user-overridden)
    CodeReview has been recorded; recording one then allows the pass. A
    non-substantive session and the AGENTCTL_CODE_REVIEW=0 knob keep only the
    free-text floor — byte-identical to the gate being absent.
    """
    from argparse import Namespace
    from agentctl import cli
    from agentctl.state import (
        Actor, CodeReview, Criterion, GateRecord, Means, Node, Outcome,
        Partition, SessionState, Stage, StageStatus, Subject,
    )

    problems: list[str] = []
    prior_code_review_env = os.environ.get("AGENTCTL_CODE_REVIEW")
    os.environ.pop("AGENTCTL_CODE_REVIEW", None)

    def _dev_stage(index=1) -> Stage:
        return Stage(
            index=index, title="dev stage",
            subject=Subject(material="m", result="img"),
            means=Means(means="Edit", method="do"),
            actor=Actor(executor="spawn:developer"),
            criterion=Criterion(criterion_type="measurable", done_criterion="dc"),
            outcome=Outcome(status=StageStatus.ACTIVE.value),
        )

    def _executing_state(stage: Stage, weight="SUBSTANTIVE") -> SessionState:
        return SessionState(
            session_id="cr-check", task_id="cr-task",
            node=Node.EXECUTING.value,
            weight_class=weight,
            route="SPAWN" if weight == "SUBSTANTIVE" else None,
            approval=GateRecord("plan_approval", armed=True, passed=True, by="user"),
            partition=Partition(verdict="not-recommended"),
            stages=[stage],
            current_stage=stage.index,
        )

    class _Mem:
        def __init__(self, s): self.s = s
        def load(self, _): return self.s
        def save(self, s): self.s = s

    try:
        # 1. SUBSTANTIVE spawn:developer + --control + passed + no CodeReview -> REFUSED
        store = _Mem(_executing_state(_dev_stage()))
        d = cli.cmd_record_result(
            Namespace(session="cr-check", status="passed", actual="done",
                      control="reviewed: self-review ok", code_ref=None),
            store=store,
        )
        if d.ok:
            problems.append(
                "record-result --status passed on a SUBSTANTIVE spawn:developer stage with "
                "--control but no recorded CodeReview was not refused (expected ok=False)"
            )
        if "code-review" not in d.detail:
            problems.append(
                f"code-review refusal directive does not name 'code-review' in its detail: {d.detail!r}"
            )

        # 2. bound passing CodeReview -> ALLOWED (transitions to VERIFYING)
        state2 = _executing_state(_dev_stage())
        state2.code_reviews.append(CodeReview(stage_index=1, verdict="pass", reviewer="code-reviewer"))
        store2 = _Mem(state2)
        d2 = cli.cmd_record_result(
            Namespace(session="cr-check", status="passed", actual="done",
                      control="reviewed: self-review ok", code_ref=None),
            store=store2,
        )
        if not d2.ok:
            problems.append(
                f"record-result --status passed with a bound passing CodeReview was refused "
                f"unexpectedly: {d2.detail}"
            )
        if d2.ok and store2.s.node != Node.VERIFYING.value:
            problems.append(
                f"after a bound passing CodeReview, node should be VERIFYING, got {store2.s.node}"
            )

        # 3. non-substantive session -> ALLOWED with only --control (byte-identical floor)
        store3 = _Mem(_executing_state(_dev_stage(), weight="SMALL_CHANGE"))
        d3 = cli.cmd_record_result(
            Namespace(session="cr-check", status="passed", actual="done",
                      control="reviewed: self-review ok", code_ref=None),
            store=store3,
        )
        if not d3.ok:
            problems.append(
                f"record-result --status passed on a non-substantive spawn:developer stage "
                f"with --control but no CodeReview was refused unexpectedly (the code-review "
                f"gate must be SUBSTANTIVE-only): {d3.detail}"
            )

        # 4. AGENTCTL_CODE_REVIEW=0 knob -> ALLOWED with only --control
        os.environ["AGENTCTL_CODE_REVIEW"] = "0"
        store4 = _Mem(_executing_state(_dev_stage()))
        d4 = cli.cmd_record_result(
            Namespace(session="cr-check", status="passed", actual="done",
                      control="reviewed: self-review ok", code_ref=None),
            store=store4,
        )
        if not d4.ok:
            problems.append(
                f"record-result --status passed with the AGENTCTL_CODE_REVIEW=0 knob was "
                f"refused unexpectedly (the knob must force the gate off): {d4.detail}"
            )
    finally:
        if prior_code_review_env is None:
            os.environ.pop("AGENTCTL_CODE_REVIEW", None)
        else:
            os.environ["AGENTCTL_CODE_REVIEW"] = prior_code_review_env
    return problems


def check_obligations() -> list[str]:
    """Verify the conformance-obligations ledger's block-then-clear behavior on
    the LIVE path (plugins.fire's generic mint() delegate -> plugin_gate_blockers'
    resolution fold), not just a direct call into the pure guardian, plus the
    STATIC force-registration pin.

    Live path: activate `review_dispatch` + `obligations` on a SUBSTANTIVE
    session at PLAN_READY with an unbound plan review, fire `submit_plan` (the
    real event review_dispatch observes) so its blocking `spawn_thinker_review`
    directive is emitted and mint() records it into the obligations ledger;
    `plugin_gate_blockers(state, 'resolution')` must then carry an
    `[obligations]`-prefixed blocker naming the obligation. Binding a passing
    PlanReview must clear it — the guardian re-checks discharge against LIVE
    state, so no re-fire is needed for the blocker to disappear.

    Static pin: `_DISCHARGE` is the single source of truth for "what is a
    conformance obligation" (mint records ONLY blocking directives whose action
    is a `_DISCHARGE` key — see plugins_obligations.py's module docstring for why
    a universal mint would deadlock resolution). This assertion is the
    force-registration safety promised in its place: a future blocking
    conformance directive that wants tracking must add a `_DISCHARGE` entry AND
    consciously update this pinned set, failing the build until it does, rather
    than a runtime fail-closed that could deadlock resolution the way a
    universal mint would.
    """
    from agentctl import plugins
    from agentctl import plugins_obligations as obligations
    from agentctl.directive import Directive
    from agentctl.state import Node, PlanReview, SessionState

    problems: list[str] = []
    prior_pr = os.environ.get("AGENTCTL_PLAN_REVIEW")
    prior_rd = os.environ.get("AGENTCTL_REVIEW_DISPATCH")
    prior_ob = os.environ.get("AGENTCTL_OBLIGATIONS")
    os.environ.pop("AGENTCTL_PLAN_REVIEW", None)
    os.environ.pop("AGENTCTL_REVIEW_DISPATCH", None)
    os.environ.pop("AGENTCTL_OBLIGATIONS", None)
    try:
        plugin = plugins.REGISTRY.get("obligations")
        if plugin is None:
            problems.append("obligations plugin not registered at import (REGISTRY)")
            return problems
        if set(plugin.gates) != {"resolution"}:
            problems.append(
                f"obligations must contribute exactly the resolution gate, got {set(plugin.gates)}"
            )
        if plugin.observers:
            problems.append(
                f"obligations must contribute no observers (mint rides fire()'s generic "
                f"post-observe delegate, not an observer of its own), got {plugin.observers}"
            )

        state = SessionState(
            session_id="obl-check", task_id="obl-task", node=Node.PLAN_READY.value,
            weight_class="SUBSTANTIVE", plan_path="/tmp/verify-agentctl-obligations-plan.toml",
        )
        plugins.activate(state, "review_dispatch")
        plugins.activate(state, "obligations")
        plugins.fire("submit_plan", state, Directive(True, state.node, "noop"))

        blockers = plugins.plugin_gate_blockers(state, "resolution")
        obligation_blockers = [b for b in blockers if b.startswith("[obligations]")]
        if not obligation_blockers:
            problems.append(
                "mint via plugins.fire('submit_plan', ...) did not populate an "
                "[obligations]-sourced resolution-gate blocker for an open, unreviewed plan review"
            )
        elif not any("spawn_thinker_review" in b for b in obligation_blockers):
            problems.append(f"obligations blocker does not name spawn_thinker_review: {obligation_blockers}")

        # A pass discharges only when reviewer-attested: write the fixture plan and
        # attest its real digest (a digest-less pass leaves the obligation open).
        import hashlib
        Path(state.plan_path).write_text("index = 1\n")
        state.plan_review = PlanReview(
            plan_path=state.plan_path, verdict="pass", reviewer="thinker",
            plan_sha256=hashlib.sha256(Path(state.plan_path).read_bytes()).hexdigest(),
        )
        cleared = plugins.plugin_gate_blockers(state, "resolution")
        Path(state.plan_path).unlink(missing_ok=True)
        if any(b.startswith("[obligations]") for b in cleared):
            problems.append(f"obligations blocker did not clear after a bound passing PlanReview: {cleared}")

        expected_discharge_actions = {"spawn_thinker_review", "spawn_code_review"}
        if set(obligations._DISCHARGE) != expected_discharge_actions:
            problems.append(
                f"obligations._DISCHARGE registry drifted from the pinned set "
                f"{expected_discharge_actions!r}, got {set(obligations._DISCHARGE)!r} — a "
                f"new/removed conformance-obligation action must be a conscious update to "
                f"this pin, not silent drift"
            )
    finally:
        for key, prior in (
            ("AGENTCTL_PLAN_REVIEW", prior_pr),
            ("AGENTCTL_REVIEW_DISPATCH", prior_rd),
            ("AGENTCTL_OBLIGATIONS", prior_ob),
        ):
            if prior is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = prior
    return problems


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--staged", action="store_true", help="ignored; accepted for verify-all uniformity")
    parser.parse_args(argv)

    from agentctl import gates, machine
    from agentctl.state import Node

    problems: list[str] = []
    problems += check_state_roundtrip()

    all_nodes = {n.value for n in Node}
    problems += check_reachability(
        all_nodes, machine.TRANSITIONS, Node.CLASSIFIED.value, SIDE_CHANNEL_REACHABLE
    )
    problems += check_dead_ends(all_nodes, machine.TRANSITIONS, TERMINAL_NODES)

    problems += check_cognitive_leaves(COGNITIVE_LEAVES, SKILLS_ROOT)

    # the gate set is taken from the engine itself so a new gate without a mapping
    # surfaces here rather than being silently unguarded
    engine_gates = set(gates.GUARDIANS)
    unmapped = sorted(engine_gates - set(GATE_TO_HOOK))
    if unmapped:
        problems.append(f"engine gate(s) with no guardian-hook mapping: {unmapped}")
    desired = parse_desired_hooks(INSTALL_SCRIPT.read_text(encoding="utf-8"))
    problems += check_gate_guardians(GATE_TO_HOOK, desired)

    problems += check_plugins()
    problems += check_control_precondition()
    problems += check_review_dispatch()
    problems += check_code_review_precondition()
    problems += check_obligations()

    if problems:
        print("verify-agentctl: FAIL")
        for p in problems:
            print(f"  - {p}")
        return 1
    print("verify-agentctl: OK — engine schema, transitions, leaves, gate guardians consistent")
    return 0


if __name__ == "__main__":
    sys.exit(main())
