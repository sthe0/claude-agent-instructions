"""A mandate's item norm that is byte-for-byte its re-render is not judged by the grounding model.

The cycle's parent session runs under `mandate.item_norm_toml`, whose principle is fixed template
text; the mandate approves the plan only when it equals the re-render from `norm.inputs.json`. That
equality is decidable, so submit-plan and the approve re-check skip the grounding judge's model part
for exactly those bytes (the given rule still runs) and judge every other plan as before.

The stub runner records each grounding-judge call and refuses every principle, so a plan that reaches
the judge is refused and a plan that does not leaves the call list empty.
"""
from __future__ import annotations

import json
from datetime import date
from pathlib import Path

import pytest

from agentctl import advisor, cli, grounding, mandate as rules, mandate_store as store
from agentctl import grounding_judge as GJ
from agentctl.dispatch import RunResult
from agentctl.plan import load_plan
from test_agentctl_mandate_basis import (  # noqa: F401 -- fixtures shared with the basis suite
    APPROVER, MANDATE_ID, World, _claude_runtime_host, _cli, _isolated_env, PARENT,
)

SOURCE = "a-controls-defects-surface-only-when-it-is-executed"
REFUSAL = "does not support the principle"


class RefusingJudge:
    def __init__(self):
        self.calls = []

    def __call__(self, argv, *, timeout=None, stdin=None, **_):
        self.calls.append(stdin or "")
        return RunResult(0, json.dumps({"defects": ["insufficient"], "reason": "stub"}), "")


def _leaf(root: Path, rel: str, schema: str, body: str, extra: str = "") -> None:
    path = root / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(f"---\nname: {path.stem}\ndescription: a leaf\nschema: {schema}\n{extra}---\n\n{body}\n",
                    encoding="utf-8")


@pytest.fixture
def world(tmp_path, monkeypatch):
    built = World(tmp_path, monkeypatch)
    leaves = tmp_path / "engine" / "memory-global" / "leaves"
    _leaf(leaves, f"regularities/{SOURCE}.md", "regularity/v1",
          "## Regularity\n\nA control's defects surface only when it is executed.\n\n## Generality\n\ng\n\n"
          "## Refutation\n\nr\n\n## Induced from\n\ne",
          f"induced_from: [experience:exp-a]\nlast_verified: {date.today().isoformat()}\n")
    _leaf(leaves, "experience/exp-a.md", "difficulty/v1", "body")
    monkeypatch.setattr(grounding, "engine_leaves_root", lambda: leaves)
    monkeypatch.setattr(GJ, "cache_dir", lambda: tmp_path / "judge-cache")
    monkeypatch.setenv("AGENTCTL_ADVISOR", "1")
    yield built
    built.close()


def _grounding_problems(plan: Path, judge: RefusingJudge) -> list[str]:
    problems = cli._submission_problems(load_plan(str(plan)), judge, "SUBSTANTIVE", plan_path=str(plan))
    return [p for p in problems if REFUSAL in p]


def test_a_rendered_mandate_norm_is_not_grounding_judged(world):
    judge = RefusingJudge()

    assert _grounding_problems(world.write_norm(), judge) == []
    assert judge.calls == []


def test_a_norm_one_byte_off_the_render_is_grounding_judged(world):
    judge = RefusingJudge()
    plan = world.write_norm(world.norm_text() + "\n")

    assert _grounding_problems(plan, judge)
    assert judge.calls


def test_a_norm_without_its_inputs_file_is_grounding_judged(world):
    judge = RefusingJudge()
    plan = world.write_norm()
    (world.item_dir / "norm.inputs.json").unlink()

    assert _grounding_problems(plan, judge)
    assert judge.calls


def test_a_norm_whose_inputs_name_another_mandate_is_grounding_judged(world):
    judge = RefusingJudge()
    plan = world.write_norm()
    other = dict(world.inputs, mandate_id="another-mandate")
    (world.item_dir / "norm.inputs.json").write_text(json.dumps(other), encoding="utf-8")

    assert _grounding_problems(plan, judge)
    assert judge.calls


def test_submit_plan_and_approve_pass_the_plan_path_on(world, monkeypatch):
    judge = RefusingJudge()
    monkeypatch.setattr(advisor, "subprocess_runner", judge)
    plan = world.write_norm()

    submitted = world.parent_to_plan_ready(plan)
    assert submitted["ok"] is True, submitted
    assert not any(REFUSAL in b for b in (submitted.get("data") or {}).get("blockers") or [])
    assert judge.calls == []

    approved = world.approve()
    assert approved["ok"] is True, approved
    assert world.parent().node == "APPROVED"
    assert judge.calls == []
