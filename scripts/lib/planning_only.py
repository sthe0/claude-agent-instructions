"""What a planning dispatch (`agentctl dispatch --subplan-only`) changes about the child it starts.

Difficulty removed: a decided-later stage is dispatched once to build its sub-plan and again to
execute it. The first child must not be able to do the second one's work, so the three wrappers
that start children (spawn-specialist.py, spawn-cursor-specialist.py, the dispatch argv) share the
one definition of "planning only": what the brief says, which of the stage's grants survive, and
the flag that carries it.
"""
from __future__ import annotations

FLAG = "--planning-only"
# The permission modes under which a child cannot write on its own say-so: an edit asks, and a
# headless child's ask is denied. Any wider mode (acceptEdits, auto, bypassPermissions, dontAsk)
# lets the planning child do the stage's work.
READ_ONLY_MODES = ("default", "plan")


def brief_lines() -> list[str]:
    """The brief section of a planning dispatch: the child is told it plans and does not
    execute, so a stage's text that reads like instructions to carry out is not one."""
    return [
        "## Planning only",
        "",
        "You were started only to plan this stage, not to execute it. Build its sub-plan in your "
        "own plan and record it; read the code you need to plan it; change no production file, "
        "run no build, test or push, and do not report the stage's result as met. Your grants "
        "are read-only for this reason: a request for more is a request to execute, and the "
        "stage is dispatched again to execute once the user has approved your sub-plan. Return "
        "`PLAN-READY:` naming the recorded sub-plan, or `INCOMPLETE:` with what is missing; never "
        "`COMPLETED:` for the stage.",
        "",
    ]


def grants(entries: "list[dict] | None") -> "list[dict] | None":
    """What a planning child keeps of the stage's grants: its read-only directories. An allow
    rule, a write directory and a VCS resource are what executing the stage needs."""
    if entries is None:
        return None
    return [e for e in entries if e.get("path") is not None and e.get("mode") == "read"]
