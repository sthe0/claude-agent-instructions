"""The model-facing prompt-site inventory (`docs/operations/llm-prompt-sites.md`).

Three controls:
  - an `ast` enumeration of `scripts/**/*.py` (tests excluded) selects every place that
    launches a model or builds a spawner's argv; its key set must equal the inventory's;
  - each inventory row names a mechanism from a closed vocabulary;
  - each row's site is run on a synthetic plan carrying a unique token at the start and end
    of every key outside `render.PROJECTED_FIELDS`, and a token of a node the row does not
    show must reach neither the captured argv nor the captured stdin.
"""
from __future__ import annotations

import ast
import inspect
import os
import re
from argparse import Namespace
from pathlib import Path
from types import SimpleNamespace

import pytest



class _LazyFixture:
    """`plan_site_fixture`, imported on first use: a tree without the module fails the
    tests that need it, not the collection of this file."""

    def __getattr__(self, name):
        import plan_site_fixture

        return getattr(plan_site_fixture, name)


fx = _LazyFixture()


def _m(node):
    return fx.token(node, "method", "A")


REPO = Path(__file__).resolve().parents[2]
INVENTORY = REPO / "docs" / "operations" / "llm-prompt-sites.md"
INSTRUCTION_HEADING = "## Instruction texts"
VOCABULARY = {"view", "brief", "inline field", "caller artifact", "no plan content"}

CALLED_SITES = {
    "_prompt_argv", "subprocess_runner", "enumerate_subprocess_runner", "build_agent_cmd",
}
SPAWNER_FILES = ("spawn-specialist.py", "spawn-cursor-specialist.py")
SPAWNER_CLI_CALL = "spawn_cli_for"

KNOWN_SITES = {
    "scripts/spawn-specialist.py:_main",
    "scripts/plan-review-topological.py:spawn_pair",
    "scripts/agentctl/dispatch.py:build_argv",
    "scripts/spawn-cursor-specialist.py:main",
    "scripts/agentctl/advisor.py:judge",
    "scripts/agentctl/advisor.py:enumerate_claims",
}


# --- inventory ----------------------------------------------------------------------


def inventory_text() -> str:
    return INVENTORY.read_text(encoding="utf-8")


def inventory_rows() -> "dict[str, dict]":
    """Rows of the tables before the `## Instruction texts` section, keyed by site."""
    text = inventory_text()
    head, _, _ = text.partition(INSTRUCTION_HEADING)
    rows: dict[str, dict] = {}
    for line in head.splitlines():
        cells = [c.strip() for c in line.strip().strip("|").split("|")]
        if len(cells) != 4 or not line.startswith("|"):
            continue
        key = cells[0].replace("`", "")
        if not re.match(r"^scripts/\S+:\S+$", key):
            continue
        rows[key] = {"plan": cells[1], "mechanism": cells[2], "bound": cells[3]}
    return rows


# --- enumeration ---------------------------------------------------------------------


def _string_elements(node) -> list:
    return [e.value if isinstance(e, ast.Constant) and isinstance(e.value, str) else None
            for e in node.elts]


def _spawner_names(tree: ast.Module) -> set:
    names = set()
    for node in tree.body:
        if (isinstance(node, ast.Assign) and len(node.targets) == 1
                and isinstance(node.targets[0], ast.Name)
                and any(name in ast.unparse(node.value) for name in SPAWNER_FILES)):
            names.add(node.targets[0].id)
    return names


class _Sites(ast.NodeVisitor):
    def __init__(self, rel: str, spawner_names: set) -> None:
        self.rel = rel
        self.spawner_names = spawner_names
        self.scope: list = []
        self.keys: set = set()

    def _enter(self, node) -> None:
        self.scope.append(node.name)
        self.generic_visit(node)
        self.scope.pop()

    visit_FunctionDef = visit_AsyncFunctionDef = visit_ClassDef = _enter

    def _key(self) -> str:
        return f"{self.rel}:{'.'.join(self.scope) or '<module>'}"

    def _argv(self, node) -> None:
        head = _string_elements(node)
        if len(head) >= 2 and head[0] == "claude" and head[1] == "-p":
            self.keys.add(self._key())
        for element in node.elts:
            for sub in ast.walk(element):
                if isinstance(sub, ast.Constant) and isinstance(sub.value, str) \
                        and sub.value.endswith(SPAWNER_FILES):
                    self.keys.add(self._key())
                elif isinstance(sub, ast.Name) and sub.id in self.spawner_names:
                    self.keys.add(self._key())
                elif isinstance(sub, ast.Call) and _callee(sub) == SPAWNER_CLI_CALL:
                    self.keys.add(self._key())
        self.generic_visit(node)

    visit_List = visit_Tuple = _argv

    def visit_Call(self, node) -> None:
        if _callee(node) in CALLED_SITES:
            self.keys.add(self._key())
        self.generic_visit(node)


def _callee(call: ast.Call):
    return getattr(call.func, "attr", getattr(call.func, "id", None))


def enumerated_sites() -> "set[str]":
    keys: set = set()
    for path in sorted((REPO / "scripts").rglob("*.py")):
        rel = path.relative_to(REPO).as_posix()
        if rel.startswith("scripts/tests/"):
            continue
        tree = ast.parse(path.read_text(encoding="utf-8"))
        visitor = _Sites(rel, _spawner_names(tree))
        visitor.visit(tree)
        keys |= visitor.keys
    return keys


def test_enumeration_equals_inventory_keys():
    selected, rows = enumerated_sites(), set(inventory_rows())
    assert selected - rows == set(), "model-facing sites without an inventory row"
    assert rows - selected == set(), "inventory rows whose site is gone"


def test_every_mechanism_is_in_the_closed_vocabulary():
    bad = {k: r["mechanism"] for k, r in inventory_rows().items() if r["mechanism"] not in VOCABULARY}
    assert bad == {}


def test_no_row_says_whole_plan():
    for key, row in inventory_rows().items():
        assert "whole plan" not in (row["plan"] + row["bound"]).lower(), key


def test_known_sites_are_still_selected():
    assert KNOWN_SITES <= enumerated_sites()
    assert KNOWN_SITES <= set(inventory_rows())


def test_instruction_texts_section_exists_and_does_not_feed_the_rows():
    text = inventory_text()
    assert INSTRUCTION_HEADING in text
    head, _, tail = (text + "\n| `scripts/not-a-site.py:nothing` | none | view | none |\n").partition(
        INSTRUCTION_HEADING)
    assert "not-a-site" in tail
    assert "not-a-site" not in head


def instruction_texts() -> "list[tuple[str, str]]":
    _, _, tail = inventory_text().partition(INSTRUCTION_HEADING)
    return re.findall(r"^- `(\S+)` — `` (.+) ``$", tail, flags=re.MULTILINE)


def test_instruction_texts_are_listed_and_each_still_appears_in_its_source():
    texts = instruction_texts()
    assert {rel for rel, _ in texts} == {
        "scripts/agentctl/render.py", "scripts/spawn-specialist.py"}
    for rel, fragment in texts:
        assert fragment in (REPO / rel).read_text(encoding="utf-8"), (rel, fragment)


def test_enumerator_selects_each_rule_on_a_synthetic_module(tmp_path):
    source = (
        "import advisor\n"
        "SPAWNER = '/x/spawn-specialist.py'\n"
        "def literal():\n    return ['claude', '-p', 'x']\n"
        "def called():\n    return advisor._prompt_argv('claude', 'low')\n"
        "def spawner_string():\n    return ['python3', 'a/spawn-cursor-specialist.py']\n"
        "def spawner_name():\n    return [sys.executable, str(SPAWNER), '--kind']\n"
        "def spawner_call():\n    return ['python3', str(spawn_cli_for(h))]\n"
        "def unrelated():\n    return ['git', 'status']\n"
    )
    tree = ast.parse(source)
    visitor = _Sites("m.py", _spawner_names(tree))
    visitor.visit(tree)
    assert visitor.keys == {
        "m.py:literal", "m.py:called", "m.py:spawner_string", "m.py:spawner_name",
        "m.py:spawner_call",
    }


# --- per-row sentinel drivers --------------------------------------------------------


class Capture:
    """A runner that records every call's argv and stdin and answers like a judge."""

    def __init__(self, answer: str = "NO") -> None:
        self.calls: list = []
        self.answer = answer

    def __call__(self, argv, *, timeout=None, stdin="", **kwargs):
        self.calls.append((list(argv), stdin or ""))
        return SimpleNamespace(returncode=0, stdout=self.answer, stderr="")

    @property
    def text(self) -> str:
        return "\n".join("\n".join(argv) + "\n" + stdin for argv, stdin in self.calls)


class Case:
    def __init__(self, label, text, shown, marker) -> None:
        self.label, self.text, self.shown, self.marker = label, text, frozenset(shown), marker


GOAL = "goal of the fixture plan"


@pytest.fixture
def plan_file(tmp_path):
    return fx.write_plan(tmp_path / "work")


@pytest.fixture
def views(tmp_path):
    return tmp_path / "views"


def _spawner():
    return fx.load_script("spawn-specialist.py", "spawn_specialist_sites_driver")


def _prompt(argv, views):
    return _spawner().assembled_prompt([*argv, "--view-root", str(views)])


def _spawn_cases(plan_file, views, argv_of) -> list:
    """The spawn-specialist prompt for each plan-site kind, `argv_of(extra)` building argv."""
    return [
        Case("dispatch-stage-brief", _prompt(argv_of("--stage-index", "2"), views), {2}, _m(2)),
        Case("dispatch-no-stage", _prompt(argv_of(), views), {"meta"}, GOAL),
        Case("whole-plan-review",
             _prompt(fx.specialist_argv(plan_file, kind="thinker"), views), {"meta"}, GOAL),
        Case("topo-pair 3-2",
             _prompt(fx.specialist_argv(plan_file, "--review-topo", "3-2", kind="thinker"), views),
             {"meta", 3}, _m(3)),
        Case("topo-pair 2-1",
             _prompt(fx.specialist_argv(plan_file, "--review-topo", "2-1", kind="thinker"), views),
             {"meta", 1, 2}, _m(1)),
        Case("topo-unit 2",
             _prompt(fx.specialist_argv(plan_file, "--review-topo", "unit:2", kind="thinker"), views),
             {2}, _m(2)),
    ]


def drive_spawn_specialist(plan_file, views, **_):
    return _spawn_cases(plan_file, views, lambda *extra: fx.specialist_argv(plan_file, *extra))


def drive_cursor(plan_file, views, monkeypatch, tmp_path, **_):
    cases = []
    for label, extra, shown, marker in (
        ("stage", ("--stage-index", "2"), {2}, _m(2)),
        ("no stage", (), {"meta", *fx.STAGES}, GOAL),
    ):
        _, prompt = fx.launch_cursor(monkeypatch, tmp_path, fx.cursor_argv(plan_file, *extra))
        assert prompt, label
        cases.append(Case(label, prompt, shown, marker))
    return cases


def drive_dispatch_build_argv(plan_file, views, **_):
    from agentctl import dispatch, plan

    doc = plan.load_plan(str(plan_file))
    stage = next(s for s in doc.stages if s.index == 2)
    argv = dispatch.build_argv(stage, str(plan_file))
    return [Case("stage 2", _prompt(argv[2:], views), {2}, _m(2))]


def drive_topo_spawn_pair(plan_file, views, monkeypatch, **_):
    topo = fx.load_script("plan-review-topological.py", "plan_review_topological_sites")
    seen: list = []

    def fake_run(cmd, **kwargs):
        seen.append(list(cmd))
        return SimpleNamespace(returncode=0, stdout="", stderr="")

    monkeypatch.setattr(topo.subprocess, "run", fake_run)
    cases = []
    for unit, shown, marker in (("3-2", {"meta", 3}, _m(3)), ("unit:2", {2}, _m(2))):
        argv = topo.build_spawn_argv(Namespace(model=None, complexity=None), unit,
                                     str(plan_file), dry_run=False, view_root=str(views))
        topo.spawn_pair(argv)
        cases.append(Case(unit, _spawner().assembled_prompt(seen[-1][2:]), shown, marker))
    return cases


def drive_topo_dry_run(plan_file, views, **_):
    topo = fx.load_script("plan-review-topological.py", "plan_review_topological_dry_sites")
    argv = topo.build_spawn_argv(Namespace(model=None, complexity=None), "3-2", str(plan_file),
                                 dry_run=False, view_root=str(views))
    return [Case("3-2", _spawner().assembled_prompt(argv), {"meta", 3}, _m(3))]


def drive_mandate_spawn_argv(plan_file, views, tmp_path, **_):
    from mandate_cycle import driver

    criterion = tmp_path / "criterion.txt"
    criterion.write_text("the work item is done", encoding="utf-8")
    (tmp_path / "wt").mkdir()
    argv = driver.Cycle.spawn_argv(None, "developer", plan_file, criterion, None,
                                   "measurable", tmp_path / "wt", "medium")
    return [Case("brief", _prompt(argv[2:], views), {"meta"}, GOAL)]


def drive_enumerate_claims(plan_file, views, **_):
    from agentctl.render import build_site_prompt

    cases = []
    for stage in (None, 1, 2, 3, 4):
        runner = Capture("")
        node = build_site_prompt("enumerate-node", plan_file, stage=stage)
        from agentctl import advisor

        advisor.enumerate_claims(node.text, runner)
        assert runner.calls, stage
        cases.append(Case(f"node {stage}", runner.text, {"meta"} if stage is None else {stage},
                          GOAL if stage is None else _m(stage)))
    return cases


def drive_enumerate_runner(plan_file, views, monkeypatch, **_):
    from agentctl import advisor

    seen: list = []

    def fake_run(argv, **kwargs):
        seen.append((list(argv), kwargs.get("input") or ""))
        return SimpleNamespace(returncode=0, stdout="", stderr="")

    monkeypatch.delenv("AGENTCTL_LIVE_JUDGES", raising=False)
    monkeypatch.setattr(advisor.subprocess, "run", fake_run)
    from agentctl.render import build_site_prompt

    node = build_site_prompt("enumerate-node", plan_file, stage=2)
    advisor.enumerate_claims(node.text, advisor.enumerate_subprocess_runner)
    assert seen
    text = "\n".join("\n".join(a) + "\n" + i for a, i in seen)
    return [Case("node 2", text, {2}, _m(2))]


def drive_judge(plan_file, views, monkeypatch, **_):
    from agentctl import advisor, plan

    doc = plan.load_plan(str(plan_file))
    payload = {"goal": doc.meta.goal, "weight_class": "substantive",
               "stages": [s.title for s in doc.stages]}
    runner = Capture("")
    kind = next(iter(advisor._PROMPTS))
    advisor.judge(kind, payload, runner, enabled=True)
    assert runner.calls
    return [Case(kind, runner.text, {"meta"}, GOAL)]


def _stage_fields(plan_file, index, *names):
    from agentctl import plan

    doc = plan.load_plan(str(plan_file))
    stage = next(s for s in doc.stages if s.index == index)
    return [str(getattr(stage, name, "") or "") for name in names]


def _raw_stage(plan_file, index) -> dict:
    import tomllib

    data = tomllib.loads(Path(plan_file).read_text(encoding="utf-8"))
    return next(s for s in data["stage"] if s["index"] == index)


def drive_judge_collapse(plan_file, views, **_):
    from agentctl import procedure

    raw = _raw_stage(plan_file, 2)
    runner = Capture("NO")
    procedure.judge_collapse(raw["method"], raw["procedure"], runner)
    assert runner.calls
    return [Case("stage 2", runner.text, {2}, _m(2))]


def drive_judge_restatement(plan_file, views, **_):
    from agentctl import conditions

    raw = _raw_stage(plan_file, 2)
    runner = Capture("NO")
    conditions.judge_restatement(raw["conditions"], runner, depends_on=[1])
    assert runner.calls
    return [Case("stage 2", runner.text, {2}, fx.token(2, "conditions", "A"))]


def drive_judge_echo(plan_file, views, **_):
    from agentctl import result_image

    raw = _raw_stage(plan_file, 2)
    runner = Capture("NO")
    result_image.judge_echo(raw["expected_result_image"], runner,
                            verify_command=raw["verify_command"],
                            done_criterion=raw["done_criterion"])
    assert runner.calls
    return [Case("stage 2", runner.text, set(), raw["expected_result_image"])]


def drive_acceptance_judge(plan_file, views, **_):
    from agentctl import advisor

    raw = _raw_stage(plan_file, 2)
    runner = Capture("NO")
    advisor.acceptance_judge("observed: file present", raw["expected_result_image"], runner,
                             enabled=True)
    assert runner.calls
    return [Case("stage 2", runner.text, set(), raw["expected_result_image"])]


def drive_question_materiality(plan_file, views, **_):
    from agentctl import advisor

    runner = Capture("NO")
    control = "the unit test of the parser gates the stage"
    question = "which parser test covers the stage control"
    advisor.judge_question_materiality(control, question, runner, enabled=True)
    assert runner.calls
    return [Case("control", runner.text, set(), question)]


def drive_invariants_coverage(plan_file, views, **_):
    from agentctl import gates

    runner = Capture("YES")
    stages = [_raw_stage(plan_file, n) for n in fx.STAGES]
    haystack = " ".join(f"{s['conditions']} {s['invariants']}" for s in stages)
    gates._semantic_invariants_coverage("an invariant nobody wrote down", haystack, runner=runner)
    assert runner.calls
    return [Case("every stage", runner.text, set(fx.STAGES), fx.token(2, "invariants", "A"))]


NO_PLAN_MARK = "NO-PLAN-CONTENT-MARKER?"
GENERIC_KWARGS = {"enabled": True, "timeout": 5, "remaining": None, "ceiling": None,
                  "runtime_host": "claude"}
GENERIC_POSITIONAL = {"name": "judge-site", "prompt_template": "{text}"}


def _rule_candidates() -> list:
    from lib import writer_rules

    rule = next(r for r in writer_rules.load_registry() if r.status == "judge")
    return [(rule.id, ["a hit line"])]


def drive_generic_judge(function):
    """A `judge_*` that takes its text positionally ahead of `runner`."""

    def driver(plan_file, views, **_):
        runner = Capture("NO")
        parameters = list(inspect.signature(function).parameters.values())
        args = []
        for parameter in parameters:
            if parameter.name == "runner":
                args.append(runner)
                break
            if parameter.name == "candidates":
                args.append(_rule_candidates())
            else:
                args.append(GENERIC_POSITIONAL.get(parameter.name, NO_PLAN_MARK))
        kwargs = {p.name: GENERIC_KWARGS[p.name]
                  for p in parameters if p.kind is p.KEYWORD_ONLY and p.name in GENERIC_KWARGS
                  and (p.default is p.empty or p.name == "enabled")}
        function(*args, **kwargs)
        assert runner.calls, function.__name__
        return [Case(function.__name__, runner.text, set(), NO_PLAN_MARK)]

    return driver


def drive_user_text(plan_file, views, **_):
    from agentctl import advisor

    runner = Capture("NO")
    advisor._judge_user_text("site", "{text}", NO_PLAN_MARK, runner, enabled=True, timeout=5,
                             remaining=None, ceiling=None, runtime_host="claude")
    assert runner.calls
    return [Case("user text", runner.text, set(), NO_PLAN_MARK)]


STRUCTURAL_ONLY = {
    "scripts/agentctl/advisor.py:judge_code_effects": "the code of a command line",
    "scripts/agentctl/cli_common.py:_venue_tree_identity.run": "a git command line",
    "scripts/agentctl/mandate.py:<module>": "a table of script names",
    "scripts/gen_crutch_registry.py:<module>": "a table key",
    "scripts/measure-marker-extractor-latency.py:_one_call": "a fixed synthetic message",
    "scripts/probe-hook-decision-semantics.py:_allow_cmd": "a fixed probe prompt",
    "scripts/probe-hook-decision-semantics.py:run_add_dir_read_cell": "a fixed probe prompt",
    "scripts/probe-hook-decision-semantics.py:run_add_dir_write_cell": "a fixed probe prompt",
    "scripts/probe-hook-decision-semantics.py:run_headless_cell": "a fixed probe prompt",
    "scripts/spawn-cursor-escape.py:main": "the difficulty statement",
    "scripts/spawn-specialist.py:_resume_for_marker": "a fixed ask for the return marker",
    "scripts/telemetry-synthetic-task.py:run_task": "a fixed list of agentctl command lines",
}

PLAN_PARAMETERS = {"plan", "plan_path", "doc", "stage", "stages", "artifact"}


def _function_node(rel: str, qualname: str):
    tree = ast.parse((REPO / rel).read_text(encoding="utf-8"))
    scope = tree
    for part in qualname.split("."):
        scope = next(
            (n for n in ast.walk(scope)
             if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)) and n.name == part),
            None,
        )
        if scope is None:
            return None
    return scope


def _generic_judges() -> dict:
    from agentctl import advisor

    names = [
        "judge_approval_ask", "judge_binary_ask", "judge_deferring_disposition",
        "judge_feedback_signal", "judge_landing_discipline_ask", "judge_outage_escalation",
        "judge_published_attachment", "judge_published_text_rules", "judge_same_difficulty",
        "judge_silent_closure",
    ]
    return {f"scripts/agentctl/advisor.py:{n}": drive_generic_judge(getattr(advisor, n))
            for n in names}


def drivers() -> dict:
    table = {
        "scripts/spawn-specialist.py:_main": drive_spawn_specialist,
        "scripts/spawn-cursor-specialist.py:main": drive_cursor,
        "scripts/agentctl/dispatch.py:build_argv": drive_dispatch_build_argv,
        "scripts/plan-review-topological.py:spawn_pair": drive_topo_spawn_pair,
        "scripts/plan-review-topological.py:Driver.dry_run": drive_topo_dry_run,
        "scripts/mandate_cycle/driver.py:Cycle.spawn_argv": drive_mandate_spawn_argv,
        "scripts/agentctl/advisor.py:enumerate_claims": drive_enumerate_claims,
        "scripts/agentctl/advisor.py:enumerate_subprocess_runner": drive_enumerate_runner,
        "scripts/agentctl/advisor.py:judge": drive_judge,
        "scripts/agentctl/advisor.py:acceptance_judge": drive_acceptance_judge,
        "scripts/agentctl/advisor.py:judge_question_materiality": drive_question_materiality,
        "scripts/agentctl/gates.py:_semantic_invariants_coverage": drive_invariants_coverage,
        "scripts/agentctl/conditions.py:judge_restatement": drive_judge_restatement,
        "scripts/agentctl/procedure.py:judge_collapse": drive_judge_collapse,
        "scripts/agentctl/result_image.py:judge_echo": drive_judge_echo,
        "scripts/agentctl/advisor.py:_judge_user_text": drive_user_text,
    }
    return table


def test_every_row_has_a_driver_or_is_structural():
    covered = set(drivers()) | set(_generic_judges()) | set(STRUCTURAL_ONLY)
    assert set(inventory_rows()) == covered


@pytest.mark.parametrize("key", sorted(STRUCTURAL_ONLY))
def test_structural_only_sites_take_no_plan_parameter(key):
    rel, qualname = key.split(":", 1)
    assert inventory_rows()[key]["mechanism"] == "no plan content"
    if qualname == "<module>":
        assert (REPO / rel).is_file()
        return
    node = _function_node(rel, qualname)
    assert node is not None, key
    if isinstance(node, ast.ClassDef):
        return
    names = {a.arg for a in [*node.args.posonlyargs, *node.args.args, *node.args.kwonlyargs]}
    assert names & PLAN_PARAMETERS == set(), key


def _all_drivers() -> dict:
    return {**drivers(), **_generic_judges()}


@pytest.mark.parametrize("key", sorted(inventory_rows()))
def test_row_site_leaks_no_token_of_a_node_it_does_not_show(key, tmp_path, monkeypatch, plan_file, views):
    if key in STRUCTURAL_ONLY:
        pytest.skip("structural only; see test_structural_only_sites_take_no_plan_parameter")
    monkeypatch.setenv("AGENTCTL_ADVISOR", "1")
    cases = _all_drivers()[key](plan_file=plan_file, views=views, monkeypatch=monkeypatch,
                                tmp_path=tmp_path)
    assert cases, key
    for case in cases:
        assert case.text.strip(), (key, case.label)
        assert case.marker in case.text, (key, case.label, "shown content missing")
        leaked = [t for t in fx.hidden_tokens(case.shown) if t in case.text]
        assert leaked == [], (key, case.label, leaked[:3])


def test_a_leaking_site_is_caught(plan_file):
    from agentctl.render import build_site_prompt

    text = build_site_prompt("dispatch-stage-brief", plan_file, stage=2).text
    assert [t for t in fx.hidden_tokens({3}) if t in text] != []


# --- ledger-enumerate -----------------------------------------------------------------


def test_ledger_enumerate_asks_one_node_per_call(store, tmp_path):
    from agentctl import cli, plugins
    from agentctl.state import SessionState

    plan_file = fx.write_plan(tmp_path / "work")
    state = SessionState(session_id="s", task_id="t")
    plugins.activate(state, "ledger")
    store.save(state)
    runner = Capture("a claim\n")
    cli.cmd_ledger_enumerate(Namespace(session="s", artifact=str(plan_file)),
                             store=store, runner=runner)
    assert len(runner.calls) == 1 + len(fx.STAGES)
    for (argv, stdin), node in zip(runner.calls, ("meta", *fx.STAGES)):
        assert [t for t in fx.hidden_tokens({node}) if t in stdin] == [], node
        assert any(t in stdin for t in fx.shown_tokens({node}) + [GOAL]), node
    stage_b = fx.token(2, "method", "A")
    call_a = runner.calls[1][1]
    assert stage_b not in call_a
    assert fx.token(1, "method", "A") in call_a


def test_ledger_enumerate_keeps_a_non_plan_artifact_whole(store, tmp_path):
    from agentctl import cli, plugins
    from agentctl.state import SessionState

    artifact = tmp_path / "deliverable.md"
    artifact.write_text("Chose approach A because latency spiked 3x.", encoding="utf-8")
    state = SessionState(session_id="s", task_id="t")
    plugins.activate(state, "ledger")
    store.save(state)
    runner = Capture("")
    cli.cmd_ledger_enumerate(Namespace(session="s", artifact=str(artifact)),
                             store=store, runner=runner)
    assert len(runner.calls) == 1
    assert "latency spiked 3x" in runner.calls[0][1]


# --- the single-call launch registry ----------------------------------------------------------
#
# `docs/operations/llm-prompt-sites.md` § Single-call registry lists every place that launches one
# model turn: the sites the `ast` finds, with the facts `lib.host_llm.SINGLE_CALL_SITES` holds for
# each. The section regenerates from the two (HOST_LLM_SITES_REGEN=1 rewrites it).

REGISTRY_HEADING = "## Single-call registry"
REGISTRY_INTRO = (
    "Every place that launches one model turn through `host_llm.launch_single_call`, or is exempt\n"
    "from it. The site set is the one the AST of `scripts/**/*.py` yields (tests, the argv builders and\n"
    "the spawners excluded); the other columns are `host_llm.SINGLE_CALL_SITES`. `denying` is computed\n"
    "from `consumer`: a hook consumer by the event of its `integrations/opencode/parity.toml` row, an\n"
    "engine consumer always, an advisory or diagnostic one never. Under OpenCode a denying site that\n"
    "cannot launch refuses and writes a `refused` ledger row; an exempt site is skipped with its reason."
)
REGISTRY_HEADER = (
    "| site | denying | consumer | legacy_host | exempt | timeout |\n"
    "|---|---|---|---|---|---|"
)
LAUNCH_CALLEES = {
    "build_launch_argv", "build_prompt_argv", "_prompt_argv", "subprocess_runner",
    "enumerate_subprocess_runner",
}
LAUNCH_HEADS = {"claude", "opencode", "agent", "cursor-agent"}
LAUNCH_SUBCOMMANDS = {"-p", "run"}
LAUNCH_EXCLUDED_FILES = {
    "scripts/lib/host_llm.py", "scripts/spawn-specialist.py", "scripts/spawn-cursor-specialist.py",
    "scripts/spawn-cursor-escape.py",
}
LAUNCH_EXCLUDED_KEYS = {
    "scripts/agentctl/advisor.py:_prompt_argv",
    "scripts/agentctl/cli_common.py:_venue_tree_identity.run",
}
PARITY_TABLE = REPO / "integrations" / "opencode" / "parity.toml"
REFUSING_EVENTS = {"PreToolUse", "PermissionRequest"}


class _Launches(ast.NodeVisitor):
    def __init__(self, rel: str) -> None:
        self.rel, self.scope, self.keys = rel, [], set()

    def _enter(self, node) -> None:
        self.scope.append(node.name)
        self.generic_visit(node)
        self.scope.pop()

    visit_FunctionDef = visit_AsyncFunctionDef = visit_ClassDef = _enter

    def _key(self) -> str:
        return f"{self.rel}:{'.'.join(self.scope) or '<module>'}"

    def visit_Call(self, node) -> None:
        if _callee(node) in LAUNCH_CALLEES:
            self.keys.add(self._key())
        self.generic_visit(node)

    def _argv(self, node) -> None:
        head = _string_elements(node)
        if len(head) >= 2 and head[0] in LAUNCH_HEADS and head[1] in LAUNCH_SUBCOMMANDS:
            self.keys.add(self._key())
        self.generic_visit(node)

    visit_List = visit_Tuple = _argv


def launch_population(root: Path = REPO) -> "set[str]":
    keys: set = set()
    for path in sorted((root / "scripts").rglob("*.py")):
        rel = path.relative_to(root).as_posix()
        if rel.startswith("scripts/tests/") or rel in LAUNCH_EXCLUDED_FILES:
            continue
        visitor = _Launches(rel)
        visitor.visit(ast.parse(path.read_text(encoding="utf-8")))
        keys |= visitor.keys
    return keys - LAUNCH_EXCLUDED_KEYS


def _facts() -> dict:
    from lib import host_llm

    return host_llm.SINGLE_CALL_SITES


def parity_events(script: str) -> "set[str]":
    import tomllib

    table = tomllib.loads(PARITY_TABLE.read_text(encoding="utf-8"))
    return {row["event"] for row in table["row"] if row["script"] == script}


def computed_denying(consumer: str, parity=parity_events) -> bool:
    """The two-source rule: a hook consumer denies when its parity row's event can refuse, an engine
    consumer denies (R1), anything else does not."""
    kind, _, rest = consumer.partition(":")
    if kind == "engine":
        return True
    if kind != "hook":
        return False
    script, _, event = rest.rpartition(":")
    if event not in parity(script):
        raise AssertionError(f"{consumer}: no parity row for {script} on {event}")
    return event in REFUSING_EVENTS


def render_registry() -> str:
    facts = _facts()
    lines = [REGISTRY_HEADING, "", REGISTRY_INTRO, "", REGISTRY_HEADER]
    for key in sorted(launch_population()):
        entry = facts.get(key)
        if entry is None:
            lines.append(f"| `{key.replace(':', '`:`', 1)}` | unregistered | | | | |")
            continue
        site, _, func = key.partition(":")
        lines.append("| " + " | ".join([
            f"`{site}`:`{func}`", "yes" if computed_denying(entry["consumer"]) and not entry["exempt"]
            else "no", entry["consumer"], entry["legacy_host"] or "-", entry["exempt"] or "-",
            f"`{entry['timeout']}`",
        ]) + " |")
    return "\n".join(lines)


def registry_section(text: str) -> str:
    _, found, tail = text.partition(REGISTRY_HEADING)
    assert found, f"{INVENTORY.name} has no `{REGISTRY_HEADING}` section"
    body, _, _ = (REGISTRY_HEADING + tail).partition(INSTRUCTION_HEADING)
    return body.rstrip()


def registry_doc_rows() -> "dict[str, list]":
    rows: dict = {}
    for line in registry_section(inventory_text()).splitlines():
        cells = [c.strip() for c in line.strip().strip("|").split("|")]
        if len(cells) == 6 and line.startswith("|") and cells[0].startswith("`scripts/"):
            rows[cells[0].replace("`", "")] = cells
    return rows


def test_registry_regenerates_identically():
    rendered = render_registry()
    if os.environ.get("HOST_LLM_SITES_REGEN") == "1":
        head, _, after = inventory_text().partition(INSTRUCTION_HEADING)
        head = head.partition(REGISTRY_HEADING)[0]
        INVENTORY.write_text(f"{head}{rendered}\n\n{INSTRUCTION_HEADING}{after}", encoding="utf-8")
    assert registry_section(inventory_text()) == rendered


def test_registry_population_matches_ast_launches():
    population, registered = launch_population(), set(_facts())
    missing, gone = sorted(population - registered), sorted(registered - population)
    assert not missing, "launch sites without a registry entry:\n" + "\n".join(missing)
    assert not gone, "registry entries whose launch site is gone:\n" + "\n".join(gone)


def test_launch_population_selects_each_rule_on_a_synthetic_tree(tmp_path):
    scripts = tmp_path / "scripts"
    (scripts / "lib").mkdir(parents=True)
    (scripts / "m.py").write_text(
        "def literal():\n    return ['claude', '-p', 'x']\n"
        "def opencode():\n    return ('opencode', 'run', 'x')\n"
        "def called():\n    return advisor.subprocess_runner(argv)\n"
        "def built():\n    return host_llm.build_launch_argv('claude', 'm')\n"
        "def unrelated():\n    return ['git', 'status']\n"
        "class K:\n    def method(self):\n        return ['agent', '-p']\n",
        encoding="utf-8")
    (scripts / "lib" / "host_llm.py").write_text("def build():\n    return ['claude', '-p']\n",
                                                 encoding="utf-8")
    assert launch_population(tmp_path) == {
        "scripts/m.py:literal", "scripts/m.py:opencode", "scripts/m.py:called",
        "scripts/m.py:built", "scripts/m.py:K.method",
    }


def test_exempt_sites_carry_reason():
    exempt = {k: v for k, v in _facts().items() if v["exempt"] is not None}
    assert exempt, "the registry names at least the diagnostic scripts as exempt"
    for key, entry in exempt.items():
        assert isinstance(entry["exempt"], str) and len(entry["exempt"].split()) >= 4, key
    rows = registry_doc_rows()
    for key in exempt:
        assert rows[key][4] == exempt[key]["exempt"], key


def test_no_denying_site_exempt():
    for key, entry in _facts().items():
        if entry["exempt"] is not None:
            assert not computed_denying(entry["consumer"]), key


def test_denying_field_two_source_rule():
    from lib import host_llm

    rows = registry_doc_rows()
    for key, entry in _facts().items():
        expected = computed_denying(entry["consumer"]) and entry["exempt"] is None
        assert host_llm.site_denying(key) is expected, key
        assert rows[key][1] == ("yes" if expected else "no"), key
        if entry["consumer"].startswith("hook:"):
            script, _, event = entry["consumer"][5:].rpartition(":")
            assert event in parity_events(script), key
    assert computed_denying("hook:hook-a.py:PreToolUse", parity=lambda script: {"PreToolUse"})
    assert not computed_denying("hook:hook-a.py:PostToolUse", parity=lambda script: {"PostToolUse"})
    assert not computed_denying("advisory:anything")
    with pytest.raises(AssertionError):
        computed_denying("hook:hook-a.py:PreToolUse", parity=lambda script: set())


def test_every_timeout_names_a_constant_of_its_source():
    for key, entry in _facts().items():
        source = (REPO / key.partition(":")[0]).read_text(encoding="utf-8")
        assert re.search(rf"\b{re.escape(entry['timeout'])}\b", source), (key, entry["timeout"])
