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
  * the argv names a session (`--session X`, `--session=X`);
  * the argv does not parse under the engine's own parser built with abbreviations
    off — an abbreviated option (`--sess`, `--b=user x`) is how a flag the checks
    below read arrives as one they do not;
  * the session has no engine state, or no `parent_link` (it is not a linked child);
  * the verb is outside OWN_CONTOUR_VERBS;
  * `approve` is not `--by agent`, or `partition` carries a marker/unit flag — the
    delivery-partition decision and every user-approved act stay the root's. These
    two are read from the parsed namespace, the one the engine then runs.

Exit 2 on a refusal; otherwise the engine's own exit code and JSON directive.
"""
from __future__ import annotations

import argparse
import contextlib
import io
import os
import sys
from pathlib import Path

SCRIPTS_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(SCRIPTS_DIR))

from agentctl.own_contour import (  # noqa: E402
    APPROVE_BY,
    OWN_CONTOUR_VERBS,
    OWN_SESSION_ENV,
    PARTITION_REFUSED_FLAGS,
)

EXIT_REFUSED = 2


def _refuse(message: str) -> int:
    print(f"agentctl-own: refused: {message}", file=sys.stderr)
    return EXIT_REFUSED


def _usage() -> str:
    return (
        "usage: agentctl-own.py <verb> [options]\n"
        "drives the engine session named by AGENTCTL_OWN_SESSION; never pass --session.\n"
        f"verbs: {', '.join(sorted(OWN_CONTOUR_VERBS))}\n"
        f"options are matched by exact spelling; approve takes --by {APPROVE_BY} only, "
        "and partition takes no --m*/--unit flag.\n"
    )


def _names_session(rest: list[str]) -> bool:
    return any(t == "--session" or t.startswith("--session=") for t in rest)


def _verb_refusal(verb: str, parsed: argparse.Namespace) -> str | None:
    if verb == "approve" and parsed.by != APPROVE_BY:
        return f"approve is available only as `approve --by {APPROVE_BY}`"
    if verb == "partition":
        # argparse's own default dest for a flag: `--m3-severe` -> m3_severe
        flags = sorted(
            f for f in PARTITION_REFUSED_FLAGS
            if getattr(parsed, f.lstrip("-").replace("-", "_"), None)
        )
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
    if _names_session(rest):
        return _refuse(
            f"this channel is pinned to the session in {OWN_SESSION_ENV} and takes no --session"
        )

    from agentctl import cli
    from agentctl.store import FileStateStore

    engine_argv = [verb, *rest, f"--session={session}"]
    diagnostics = io.StringIO()
    try:
        with contextlib.redirect_stderr(diagnostics):
            parsed = cli.build_parser(allow_abbrev=False).parse_args(engine_argv)
    except SystemExit as exit_:
        if exit_.code in (0, None):
            return 0
        detail = diagnostics.getvalue().strip().splitlines()
        return _refuse(
            f"{detail[-1] if detail else 'the arguments do not parse'} (options are matched "
            "by exact spelling, and this channel takes no --session)"
        )
    reason = _verb_refusal(verb, parsed)
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
    return cli.main(engine_argv, allow_abbrev=False)


if __name__ == "__main__":
    sys.exit(main())
