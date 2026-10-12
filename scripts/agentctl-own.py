"""The own-contour channel of a linked child: `agentctl` on ONE fixed session, a fixed verb set.

Difficulty removed: a child spawned for a parent stage is gated by its own engine
session (scripts/hook-state-gate.py), so it must move that session itself —
`classify`, `plan`, `submit-plan`, `approve --by agent`, `next-stage` — yet most of
those verbs are user-authority verbs that no permission rule may grant (an allow
rule over `agentctl-cli.py` cannot fix the session, because argparse honours the
last `--session` given). This script is the bounded channel instead: a spawn grants
it by exact path and exports `AGENTCTL_OWN_SESSION=<the child's session id>`; the
script places `--session` itself and runs only the verbs in OWN_CONTOUR_VERBS.

It refuses, and never reaches the engine, when:
  * AGENTCTL_OWN_SESSION is unset (the caller is not a linked child's own shell);
  * the argv names a session in any form (`--session X`, `--session=X`, `--sess X`),
    or any option the verb does not declare under that exact spelling — an
    abbreviation is how a flag the wrapper checks arrives as one it does not;
  * the session has no engine state, or no `parent_link` (it is not a linked child);
  * the verb is outside OWN_CONTOUR_VERBS;
  * `approve` is not `--by agent`, or `partition` carries a marker/unit flag — the
    delivery-partition decision and every user-approved act stay the root's.

Exit 2 on a refusal; otherwise the engine's own exit code and JSON directive.
"""
from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path

SCRIPTS_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(SCRIPTS_DIR))

OWN_SESSION_ENV = "AGENTCTL_OWN_SESSION"

OWN_CONTOUR_VERBS = frozenset({
    "classify", "status", "plan", "next-stage", "submit-plan", "plan-review",
    "plan-review-delta", "approve", "partition", "dispatch", "record-result",
    "code-review", "verify-final", "declare", "investigate", "critique",
    "normalize", "replan", "stage-grants", "plan-grants", "question-raise",
    "question-dispose",
})

PARTITION_REFUSED_FLAGS = frozenset({
    "--m1", "--m2", "--m3", "--m4", "--m3-severe", "--m4-severe", "--unit",
})

EXIT_REFUSED = 2


def _refuse(message: str) -> int:
    print(f"agentctl-own: refused: {message}", file=sys.stderr)
    return EXIT_REFUSED


def _usage() -> str:
    return (
        "usage: agentctl-own.py <verb> [options]\n"
        "drives the engine session named by AGENTCTL_OWN_SESSION; never pass --session.\n"
        f"verbs: {', '.join(sorted(OWN_CONTOUR_VERBS))}\n"
        "approve takes --by agent only; partition takes no --m*/--unit flag.\n"
    )


def _declared_options(parser: argparse.ArgumentParser, verb: str) -> frozenset[str] | None:
    """The exact option strings the engine's `verb` subparser declares, else None."""
    for action in parser._actions:
        if isinstance(action, argparse._SubParsersAction):
            sub = action.choices.get(verb)
            if sub is None:
                return None
            return frozenset(s for a in sub._actions for s in a.option_strings)
    return None


def _option_refusal(rest: list[str], declared: frozenset[str]) -> str | None:
    for token in rest:
        if not token.startswith("-") or len(token) < 2 or " " in token:
            continue
        if token == "--":
            return "`--` is not accepted"
        if token.lstrip("-").replace(".", "", 1).isdigit():
            continue
        name = token.split("=", 1)[0]
        if len(name) >= 3 and "--session".startswith(name):
            return (
                f"{token!r} names a session; this channel is pinned to the session in "
                f"{OWN_SESSION_ENV} and takes no --session"
            )
        if name not in declared:
            return f"option {name!r} is not an exact option of this verb (abbreviations are refused)"
    return None


def _verb_refusal(verb: str, rest: list[str]) -> str | None:
    names = {t.split("=", 1)[0] for t in rest if t.startswith("--")}
    if verb == "approve":
        probe = argparse.ArgumentParser(add_help=False, allow_abbrev=False)
        probe.add_argument("--by")
        by = probe.parse_known_args(rest)[0].by
        if by != "agent":
            return "approve is available only as `approve --by agent`"
    if verb == "partition":
        flags = sorted(names & PARTITION_REFUSED_FLAGS)
        if flags:
            return (
                f"partition takes no {', '.join(flags)}: the delivery-partition decision "
                "stays the root's"
            )
    return None


def main(argv: list[str] | None = None) -> int:
    args = list(sys.argv[1:] if argv is None else argv)
    if args and args[0] in ("-h", "--help"):
        print(_usage(), end="")
        return 0
    session = os.environ.get(OWN_SESSION_ENV, "").strip()
    if not session:
        return _refuse(f"{OWN_SESSION_ENV} is not set; this channel exists only for a linked child")
    if not args or args[0].startswith("-"):
        return _refuse("the first argument must be a verb\n" + _usage().rstrip())
    verb, rest = args[0], args[1:]
    if verb not in OWN_CONTOUR_VERBS:
        return _refuse(f"verb {verb!r} is outside the own contour; return a marker to your parent instead")

    from agentctl import cli
    from agentctl.store import FileStateStore

    declared = _declared_options(cli.build_parser(), verb)
    if declared is None:
        return _refuse(f"verb {verb!r} is not an agentctl command")
    reason = _option_refusal(rest, declared) or _verb_refusal(verb, rest)
    if reason is not None:
        return _refuse(reason)

    state = FileStateStore().load(session)
    if state is None:
        return _refuse(f"no engine session {session!r}; the child's engine has not started")
    if state.parent_link is None:
        return _refuse(
            f"session {session!r} holds no norm handoff (parent_link): it is not a linked "
            "child, so it has no own contour to drive through this channel"
        )
    return cli.main([verb, *rest, "--session", session])


if __name__ == "__main__":
    sys.exit(main())
