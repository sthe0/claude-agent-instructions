"""Session and plan builders for the risk-shaped-planning phase-A tests.

`skeleton_text` turns the two-stage fixture into a skeleton plan; `Scenario` drives a
session through `cli.main` (the `Eng` harness) with the binding chosen by the caller.
"""
from __future__ import annotations

import hashlib
from pathlib import Path

from agentctl.state import SessionState
from test_replan_autonomy_boundary import Eng, plan_text

SKELETON_META = 'phase = "skeleton"\nbase_stage = {base}\n{alts}'
WAIVER = 'alternatives_waiver = "one chain reaches the goal"\n'
SESSION = "sk1"


def skeleton_text(*, base_stage: int = 2, alternatives: str = WAIVER, stage3: bool = False,
                  edits: tuple[tuple[str, str], ...] = ()) -> str:
    """The two-stage fixture marked as a skeleton. `edits` are (old, new) replacements applied
    to the finished text, so a test changes exactly one thing."""
    text = plan_text(stage3=stage3)
    meta = SKELETON_META.format(base=base_stage, alts=alternatives)
    text = text.replace('task_id = "demo-two-stage"\n', 'task_id = "demo-two-stage"\n' + meta, 1)
    for old, new in edits:
        assert old in text, old
        text = text.replace(old, new, 1)
    return text


class Scenario:
    def __init__(self, tmp_path: Path):
        self.eng = Eng(tmp_path)

    def write(self, text: str, name: str | None = None) -> str:
        return self.eng.write(text, name)

    def submit(self, plan: str, sid: str = SESSION) -> dict:
        """First call opens the session (start, classify, plan) and submits; later calls on
        the same session resubmit at PLAN_READY."""
        if self.eng.store.load(sid) is None:
            return self.eng.open(sid, plan)
        return self.eng.run("submit_plan", session=sid, plan=plan)

    def review(self, plan: str, verdict: str = "pass", *, note: str | None = None,
               concerns: tuple[str, ...] = (), sid: str = SESSION) -> dict:
        digest = hashlib.sha256(Path(plan).read_bytes()).hexdigest()
        return self.eng.run("plan_review", session=sid, verdict=verdict, reviewer="thinker",
                            target=plan, plan_digest=digest, note=note,
                            concern=list(concerns) or None)

    def state(self, sid: str = SESSION) -> SessionState:
        return self.eng.state(sid)
