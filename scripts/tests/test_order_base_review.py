"""The standard review of the plan base node judges against the order the user agreed.

Claims, each pinned in the direction that could fail:

* the `unit:base` bundle inlines the order's activity, difficulty, functional place,
  requirements with derivations, proposals (id, text, verdict, critique, against) and
  constraints, and its conditions C1-C4 follow the phase (a draft or a staged plan);
* every `base-<s>` section lists all rejected proposals and the adopted or adapted ones
  that stand against one of that edge's requirements, and asks that the stage realize no
  rejected proposal;
* the label "User-agreed order understanding" appears in `unit:base` and in every
  `base-<s>` section only when the digest passed equals the plan's own understanding digest;
* the spawner loads an order draft for `unit:base` and hands the digest to the renderer;
* the driver walks exactly `unit:base` for a draft, reads the digest from `agentctl status`,
  forwards it to every `unit:base` and `base-<s>` spawn (and no other), and records a
  draft's verdict with scope `order` and the reviewer's transcript.

Each clause is pinned by a planted defect in `order_agreement_mutants`;
`test_agreement_mutant_is_killed` re-runs this file under every one and asserts pytest exit 1
with the intended clause test among the failures. Code a mutant replaces is reached through
its module, never a by-name import.
"""
from __future__ import annotations

import argparse
import contextlib
import hashlib
import io
import json
import os
import subprocess
import sys
from pathlib import Path

import pytest
from order_agreement_mutants import ENV, MUTANTS, apply_active_mutant, script_module
from test_order_phase import _draft
from test_plan_review_topo import _data, _write
from test_plan_review_topological import FakeEngine, canonical_stdout, review_original, summary_line

from agentctl import cli, plan, render
from agentctl.store import FileStateStore

SCRIPTS = Path(__file__).resolve().parent.parent
THIS_FILE = Path(__file__).resolve()
LABEL = "User-agreed order understanding (digest "

drv = script_module("plan-review-topological.py")
spawner = script_module("spawn-specialist.py")

PROPOSALS = [
    {"id": "P1", "text": "tag releases by hand", "verdict": "reject",
     "critique": "that is the hand-work", "against": ["R2"]},
    {"id": "P2", "text": "a command tags releases", "verdict": "adopt",
     "critique": "cheap and reversible", "against": ["R1"]},
    {"id": "P3", "text": "a bot tags releases", "verdict": "adapt",
     "critique": "only on the release branch", "against": ["R2"]},
]
ORDER_FIELDS = dict(
    activity="release engineering",
    difficulty="a release takes a day of hand-work",
    functional_place="the norm governing releases",
    requirements=[
        {"id": "R1", "text": "releases are tagged by a command", "derivation": "removes the hand-work"},
        {"id": "R2", "text": "a tag names its commit", "derivation": "a release must be traceable"},
    ],
    coverage={"R1": ["stage 3 verify_command"], "R2": ["stage 1 verify_command"]},
    proposals=PROPOSALS,
    constraints=[{"id": "K1", "text": "no new dependency"}],
)


@pytest.fixture(autouse=True)
def mutant(monkeypatch):
    return apply_active_mutant(monkeypatch)


@pytest.fixture
def staged_plan(tmp_path) -> Path:
    return _write(tmp_path / "plan.toml", _with_order_functional_place(_data(**ORDER_FIELDS)))


def _with_order_functional_place(data: dict) -> dict:
    order = data["meta"]["order"]
    order["functional_place"] = ORDER_FIELDS["functional_place"]
    return data


@pytest.fixture
def draft(tmp_path) -> Path:
    return _draft(tmp_path)


def _digest(path: Path) -> str:
    return plan.understanding_digest(plan.load_plan(path))


def _site(kind: str, path: Path, tmp_path: Path, agreed=None, **selector) -> str:
    return render.build_site_prompt(
        kind, path, view_root=tmp_path / "views", agreed_understanding=agreed, **selector).text


def _unit_text(path, tmp_path, agreed=None) -> str:
    return _site("topo-unit", path, tmp_path, agreed, unit="unit:base")


def _pair_text(path, tmp_path, agreed=None, pair="base-3") -> str:
    return _site("topo-pair", path, tmp_path, agreed, pair=pair)


def test_unit_base_bundle_inlines_activity_difficulty_place_requirements_proposals_constraints(
        staged_plan, tmp_path):
    text = _unit_text(staged_plan, tmp_path)
    for expected in (
        "release engineering", "a release takes a day of hand-work", "the norm governing releases",
        "releases are tagged by a command", "removes the hand-work",
        "P1", "tag releases by hand", "reject", "that is the hand-work",
        "P2", "a command tags releases", "adopt", "cheap and reversible",
        "P3", "only on the release branch", "K1", "no new dependency",
    ):
        assert expected in text, expected


def test_unit_base_bundle_of_a_draft_inlines_the_order_and_its_proposals(draft, tmp_path):
    text = _unit_text(draft, tmp_path)
    for expected in ("release engineering", "tag releases automatically", "adopt", "cheap, reversible",
                     "no new dependency"):
        assert expected in text, expected


def test_unit_base_bundle_of_a_draft_says_coverage_does_not_apply(draft, tmp_path):
    text = _unit_text(draft, tmp_path)
    assert "Coverage: not applicable to an order draft" in text
    assert "## Order coverage" not in text
    assert "(none declared)" not in text and "Requires traceability" not in text
    assert "none at the order phase" in text


def test_unit_base_bundle_of_a_staged_plan_keeps_the_coverage_map(staged_plan, tmp_path):
    text = _unit_text(staged_plan, tmp_path)
    assert "## Order coverage" in text
    assert "R1: stage 3 verify_command" in text and "R2: stage 1 verify_command" in text
    assert "not applicable to an order draft" not in text
    assert "R1 → stage 3" in text


def test_unit_base_conditions_follow_the_phase(staged_plan, draft, tmp_path):
    staged = _unit_text(staged_plan, tmp_path)
    drafted = _unit_text(draft, tmp_path)
    for marker in plan.CONDITION_MARKERS:
        assert marker in staged and marker in drafted
    assert "every constraint is respected by the coverage entries and the final checks" in staged
    assert "every proposal's verdict follows from its critique" in staged
    c1, c2, c3, c4 = (_condition(drafted, i) for i in range(4))
    assert "the activity is reconstructed and the difficulty is placed in it" in c1
    assert "no requirement merely restates a proposal" in c2
    assert "each proposal's critique holds against the requirements it names" in c2
    assert "every constraint is user-stated and offered, not decided by the author" in c3
    assert "coverage entries" not in c3 and "coverage entries" not in c4


def _condition(text: str, index: int) -> str:
    """The body of condition `index` (0-based) of a bundle, up to the next marker."""
    markers = plan.CONDITION_MARKERS
    text = text.rsplit("\nConditions:\n", 1)[1]
    body = text.split(f"- `{markers[index]}`", 1)[1]
    return body.split(f"`{markers[index + 1]}`", 1)[0] if index + 1 < len(markers) else body


def test_unit_base_bundle_labels_the_activity_difficulty_and_functional_place(staged_plan, tmp_path):
    text = _unit_text(staged_plan, tmp_path)
    assert "**Activity:** release engineering" in text
    assert "**Difficulty:** a release takes a day of hand-work" in text
    assert "**Functional place:** the norm governing releases" in text


def test_staged_c3_names_constraints_only_when_the_plan_has_them(staged_plan, tmp_path):
    clause = "every constraint is respected by the coverage entries"
    assert clause in _condition(_unit_text(staged_plan, tmp_path), 2)
    data = _with_order_functional_place(_data(**{**ORDER_FIELDS, "constraints": []}))
    bare = _unit_text(_write(tmp_path / "no-constraints.toml", data), tmp_path)
    assert "constraint" not in _condition(bare, 2)
    assert "none is left uncovered" in _condition(bare, 2)


def test_plain_plan_unit_base_bundle_carries_no_understanding_section_and_no_label(tmp_path):
    path = _write(tmp_path / "plain.toml", _data())
    text = _unit_text(path, tmp_path, agreed=None)
    assert "## Order understanding" not in text and LABEL not in text


def test_pair_section_lists_every_rejected_proposal_and_the_adopted_ones_bearing_on_the_edge(
        staged_plan, tmp_path):
    section = _pair_text(staged_plan, tmp_path)
    assert "Proposals bearing on this edge" in section
    assert "P1" in section and "tag releases by hand" in section
    assert "P2" in section and "a command tags releases" in section
    assert "a bot tags releases" not in section


def test_pair_section_lists_the_proposals_of_its_own_edge_only(
        staged_plan, tmp_path):
    section = _pair_text(staged_plan, tmp_path, pair="base-1")
    assert "P1" in section and "tag releases by hand" in section
    assert "P3" in section and "a bot tags releases" in section
    assert "a command tags releases" not in section


def test_stage_pair_section_carries_no_proposals_and_no_proposal_clause(staged_plan, tmp_path):
    section = _pair_text(staged_plan, tmp_path, pair="3-1")
    assert "Proposals bearing on this edge" not in section
    assert "tag releases by hand" not in section
    assert "realizes no rejected proposal" not in section


def test_pair_section_lists_a_rejected_proposal_against_any_requirement(staged_plan, tmp_path):
    data = _data(**{**ORDER_FIELDS, "proposals": [{**PROPOSALS[0], "against": ["R2"]}]})
    path = _write(tmp_path / "rejected-only.toml", _with_order_functional_place(data))
    assert "tag releases by hand" in _pair_text(path, tmp_path)


def test_pair_condition_three_asks_that_no_rejected_proposal_is_realized(staged_plan, tmp_path):
    section = _pair_text(staged_plan, tmp_path)
    assert "realizes no rejected proposal listed above" in section
    assert "only as its verdict says" in section


@pytest.mark.parametrize("site", ["unit", "pair"])
def test_label_appears_only_when_the_passed_digest_equals_the_plans(staged_plan, tmp_path, site):
    build = _unit_text if site == "unit" else _pair_text
    digest = _digest(staged_plan)
    assert f"{LABEL}{digest[:12]})" in build(staged_plan, tmp_path, agreed=digest)
    assert LABEL not in build(staged_plan, tmp_path, agreed="0" * 64)
    assert LABEL not in build(staged_plan, tmp_path, agreed=None)


def test_label_names_a_defect_in_the_agreement_as_an_understanding_concern(staged_plan, tmp_path):
    text = _unit_text(staged_plan, tmp_path, agreed=_digest(staged_plan))
    assert "judge the stage against it" in text and "`understanding:` concern" in text


def test_label_is_absent_from_a_stage_unit_whatever_digest_is_passed(staged_plan, tmp_path):
    text = _site("topo-unit", staged_plan, tmp_path, _digest(staged_plan), unit="unit:3")
    assert LABEL not in text


def test_spawner_loads_an_order_draft_and_a_plan(draft, staged_plan):
    doc, sha = spawner.load_review_doc(str(draft))
    assert not doc.stages and len(sha) == 64
    doc, sha = spawner.load_review_doc(str(staged_plan))
    assert doc.stages and len(sha) == 64


def test_spawner_prompt_for_a_draft_unit_base_carries_the_label_for_the_agreed_digest(
        draft, tmp_path):
    digest = plan.understanding_digest(plan.load_order_draft(draft)[0])
    args = argparse.Namespace(kind="thinker", plan=str(draft), review_topo="unit:base",
                              agreed_understanding=digest)
    assert LABEL in spawner.plan_site(args, view_root=tmp_path / "views").text
    args.agreed_understanding = None
    assert LABEL not in spawner.plan_site(args, view_root=tmp_path / "views").text


def _spawn_flags(plan_path: Path, *extra: str) -> list[str]:
    return ["--kind", "thinker", "--complexity", "low", "--effort", "low", "--plan", str(plan_path),
            "--done-criterion", "d", "--criterion-type", "acceptance-review", *extra]


def test_spawner_parser_accepts_agreed_understanding(draft):
    parsed = spawner.build_parser().parse_args(
        _spawn_flags(draft, "--review-topo", "unit:base", "--agreed-understanding", "ab" * 32))
    assert parsed.agreed_understanding == "ab" * 32


def test_spawner_refuses_agreed_understanding_without_review_topo(draft, capsys):
    rc = spawner.main(_spawn_flags(draft, "--agreed-understanding", "ab" * 32, "--dry-run"))
    assert rc == 2 and "only valid with --review-topo" in capsys.readouterr().err


def test_driver_walks_exactly_unit_base_for_a_draft(draft):
    d = drv.Driver(argparse.Namespace(session="s", pairs=None), str(draft), {})
    walk = d.read_walk()
    assert [row["pair"] for level in walk["levels"] for row in level] == ["unit:base"]
    assert len(walk["levels"]) == 1 and walk["levels"][0][0]["status"] == "missing"


def _spawn_argv(pair, digest, plan_path="p.toml"):
    return drv.build_spawn_argv(argparse.Namespace(model=None, complexity=None), pair, plan_path,
                                dry_run=False, agreed_understanding=digest)


def test_driver_forwards_the_agreement_digest_to_the_unit_base_spawn():
    argv = _spawn_argv("unit:base", "ab" * 32)
    assert argv[argv.index("--agreed-understanding") + 1] == "ab" * 32


def test_driver_forwards_the_agreement_digest_to_every_base_pair_spawn():
    argv = _spawn_argv("base-3", "ab" * 32)
    assert argv[argv.index("--agreed-understanding") + 1] == "ab" * 32


@pytest.mark.parametrize("pair", ["unit:3", "3-1"])
def test_driver_does_not_forward_the_agreement_digest_to_stage_reviews(pair):
    assert "--agreed-understanding" not in _spawn_argv(pair, "ab" * 32)


def test_driver_forwards_no_digest_when_none_is_agreed():
    assert "--agreed-understanding" not in _spawn_argv("unit:base", None)
    assert "--agreed-understanding" not in _spawn_argv("base-3", None)


@pytest.mark.parametrize("data, expected", [
    ({"order_agreement": {"understanding_sha256": "cd" * 32, "at": 1.0, "anchor": "a"}}, "cd" * 32),
    ({"order_agreement": None}, None),
])
def test_driver_reads_the_agreement_digest_from_agentctl_status(monkeypatch, data, expected):
    seen = []

    def fake(argv, env, script=drv.AGENTCTL_CLI):
        seen.append(argv)
        return {"ok": True, "data": data}

    monkeypatch.setattr(drv, "run_agentctl", fake)
    d = drv.Driver(argparse.Namespace(session="s", pairs=None), "missing.toml", {})
    assert d.read_agreement() == expected
    assert seen == [["status", "--session", "s"]]


def test_driver_records_a_draft_verdict_with_scope_order_and_the_transcript():
    argv = drv.record_argv("s", "d.toml", "unit:base", "pass", "ab" * 32, [],
                           draft=True, transcript="/t/transcript.jsonl")
    assert argv[argv.index("--scope") + 1] == "order"
    assert argv[argv.index("--transcript") + 1] == "/t/transcript.jsonl"


def test_driver_records_a_plan_verdict_with_the_topo_scope_and_no_transcript():
    argv = drv.record_argv("s", "p.toml", "base-3", "pass", "ab" * 32, [], transcript="/t")
    assert argv[argv.index("--scope") + 1] == "topo:base-3" and "--transcript" not in argv


def test_driver_settle_of_a_draft_review_records_it_under_scope_order(draft, monkeypatch):
    recorded = []
    monkeypatch.setattr(drv, "run_agentctl",
                        lambda argv, env, script=None: recorded.append(argv) or {"ok": True, "data": {}})
    monkeypatch.setattr(drv, "pair_telemetry", lambda launched, pair: (None, None, None, "/t/tr.jsonl"))
    sha = hashlib.sha256(draft.read_bytes()).hexdigest()
    stdout = canonical_stdout(review_original("pass", sha))
    d = drv.Driver(argparse.Namespace(session="s", pairs=None), str(draft), {})
    d.settle("unit:base", drv.Launched(0, stdout, summary_line() + "\n", 0, sha))
    (argv,) = [a for a in recorded if a[0] == "plan-review"]
    assert argv[argv.index("--scope") + 1] == "order"
    assert argv[argv.index("--transcript") + 1] == "/t/tr.jsonl"


def _run_driver(monkeypatch, tmp_path, plan_path, engine):
    """Run the driver's `main` in-process against `engine`, every spawn refused (rc 1);
    return the spawner argvs it built."""
    spawns = []
    monkeypatch.setattr(drv, "spawn_pair", lambda argv: spawns.append(list(argv)) or (1, "", ""))
    monkeypatch.setattr(drv, "run_agentctl", engine.run)
    monkeypatch.setattr(drv, "COST_LOG", tmp_path / "costs.jsonl")
    monkeypatch.setenv("AGENTCTL_TOPO_UNITS_DIR", str(tmp_path / "topo"))
    drv.main(["--session", "s", "--plan", str(plan_path)])
    return spawns


def _digest_flag(argv):
    return argv[argv.index("--agreed-understanding") + 1] if "--agreed-understanding" in argv else None


def test_driver_run_hands_the_status_digest_to_unit_base_and_every_base_pair(
        staged_plan, tmp_path, monkeypatch):
    engine = FakeEngine([["unit:base"], ["base-1", "base-3"]])
    engine.agreement = {"understanding_sha256": "ab" * 32}
    spawns = _run_driver(monkeypatch, tmp_path, staged_plan, engine)
    assert sorted(a[a.index("--review-topo") + 1] for a in spawns) == ["base-1", "base-3", "unit:base"]
    assert {_digest_flag(a) for a in spawns} == {"ab" * 32}


def test_driver_run_of_a_draft_hands_no_digest_even_when_status_has_an_agreement(
        draft, tmp_path, monkeypatch):
    engine = FakeEngine([])
    engine.agreement = {"understanding_sha256": "cd" * 32}
    (argv,) = _run_driver(monkeypatch, tmp_path, draft, engine)
    assert argv[argv.index("--review-topo") + 1] == "unit:base"
    assert _digest_flag(argv) is None


def test_driver_run_hands_no_digest_when_status_has_no_agreement(staged_plan, tmp_path, monkeypatch):
    engine = FakeEngine([["unit:base"], ["base-3"]])
    spawns = _run_driver(monkeypatch, tmp_path, staged_plan, engine)
    assert spawns and {_digest_flag(a) for a in spawns} == {None}


def test_driver_compose_of_a_draft_records_an_order_review_naming_the_transcript(
        draft, tmp_path, monkeypatch):
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(tmp_path))
    state_root = tmp_path / "agentctl" / "state"
    store = FileStateStore(state_root)
    cli.cmd_start(argparse.Namespace(session="s", task="od", goal="g", done_criterion="dc",
                                     criterion_type="measurable", recursion_depth=0), store=store)

    def engine(argv, env, script=None):
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            cli.main(["--state-root", str(state_root), *argv])
        return json.loads(buf.getvalue())

    monkeypatch.setattr(drv, "run_agentctl", engine)
    monkeypatch.setattr(drv, "pair_telemetry", lambda launched, pair: (None, None, None, "/t/tr.jsonl"))
    sha = hashlib.sha256(draft.read_bytes()).hexdigest()
    d = drv.Driver(argparse.Namespace(session="s", pairs=None), str(draft), {})
    d.settle("unit:base", drv.Launched(0, canonical_stdout(review_original("pass", sha)),
                                       summary_line() + "\n", 0, sha))
    assert d.compose() == "pass"
    (review,) = store.load("s").order_reviews
    assert review.verdict == "pass" and review.transcript == "/t/tr.jsonl"
    assert review.understanding_sha256 == plan.understanding_digest(plan.load_order_draft(draft)[0])


def test_driver_dry_run_of_an_order_draft_lists_unit_base_and_refuses_nothing(
        draft, tmp_path):
    env = {k: v for k, v in os.environ.items() if k != ENV}
    (tmp_path / "skills").mkdir()
    (tmp_path / "skills" / "thinker").symlink_to(SCRIPTS.parent / "skills" / "specializations" / "thinker")
    env.update(CLAUDE_CONFIG_DIR=str(tmp_path), AGENTCTL_TOPO_UNITS_DIR=str(tmp_path / "topo"))
    run = subprocess.run(
        [sys.executable, str(SCRIPTS / "plan-review-topological.py"), "--session", "s",
         "--plan", str(draft), "--dry-run"],
        cwd=SCRIPTS, capture_output=True, text=True, env=env, timeout=120)
    assert run.returncode == 0, run.stdout[-800:] + run.stderr[-800:]
    assert "TOPO-DRY: pair=unit:base" in run.stdout
    assert "TOPO-REFUSED" not in run.stdout


KILLERS = {
    "driver_agreement_unread": "test_driver_run_hands_the_status_digest_to_unit_base_and_every_base_pair",
    "rejected_proposal_filtered": "test_pair_section_lists_a_rejected_proposal_against_any_requirement",
    "unit_base_omits_proposals": "test_unit_base_bundle_inlines_activity_difficulty_place_requirements_proposals_constraints",
    "draft_walk_empty": "test_driver_walks_exactly_unit_base_for_a_draft",
    "agreed_label_unconditional": "test_label_appears_only_when_the_passed_digest_equals_the_plans",
    "agreed_digest_not_forwarded": "test_driver_forwards_the_agreement_digest_to_the_unit_base_spawn",
    "pair_digest_not_forwarded": "test_driver_forwards_the_agreement_digest_to_every_base_pair_spawn",
    "spawner_digest_dropped":
        "test_spawner_prompt_for_a_draft_unit_base_carries_the_label_for_the_agreed_digest",
    "spawner_refuses_draft": "test_spawner_loads_an_order_draft_and_a_plan",
    "draft_recorded_as_topo": "test_driver_settle_of_a_draft_review_records_it_under_scope_order",
    "draft_coverage_rendered": "test_unit_base_bundle_of_a_draft_says_coverage_does_not_apply",
    "agreed_forwarded_on_draft":
        "test_driver_run_of_a_draft_hands_no_digest_even_when_status_has_an_agreement",
    "pair_proposal_clause_omitted": "test_pair_condition_three_asks_that_no_rejected_proposal_is_realized",
    "adopted_proposal_unfiltered":
        "test_pair_section_lists_the_proposals_of_its_own_edge_only",
}


@pytest.mark.parametrize("name", sorted(KILLERS))
def test_agreement_mutant_is_killed(name):
    assert name in MUTANTS
    run = subprocess.run(
        [sys.executable, "-m", "pytest", str(THIS_FILE), "-q", "-p", "no:cacheprovider", "-rf",
         "-k", "not test_agreement_mutant_is_killed"],
        cwd=SCRIPTS, capture_output=True, text=True, env={**os.environ, ENV: name}, timeout=300,
    )
    assert run.returncode == 1, run.stdout[-1500:] + run.stderr[-500:]
    assert f"::{KILLERS[name]}" in run.stdout, run.stdout[-1500:]
