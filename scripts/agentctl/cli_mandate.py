"""Task-accumulator reset and standing-mandate subcommands.

Moved verbatim from cli.py; see cli.py for the command table."""
from __future__ import annotations

import argparse
from . import mandate as mandate_rules, mandate_store, task_accumulator
from .directive import Directive, DIRECTIVE_ESCALATE_TO_USER
from .dispatch import Runner
from .state import AGENT_ACTOR
from .store import StateStore
from .cli_common import _MANDATE_NODE, _mandate_by_refusal, _mandate_view, _utcnow


def cmd_task_reset(args, *, store: StateStore, runner: Runner | None = None) -> Directive:
    """Explicit renegotiation: zero the cross-session task accumulator (item B)
    for `--task`. `cmd_reset` deliberately does NOT clear it, since the
    accumulator is task-scoped, not session-scoped: a fresh session re-armed
    on the same stuck task must inherit its prior friction, not silently
    forgive it (that would defeat the accumulator's entire purpose).
    Session-independent by design (no `--session`, no state load) — the
    accumulator lives outside any single session's state file — and requires
    `--reason` so this is never a casual one-flag habit; a user genuinely
    renegotiating a task's scope states why. A second, in-session path exists
    for the same explicit-renegotiation act: `cmd_replan`'s
    `--renegotiation-decision continue|rescope` (see
    `task_accumulator.reset`'s own docstring) — that path folds the reset into
    an already-required customer decision instead of a separate command."""
    by = (args.by or "").strip()
    if not by or by.casefold() == AGENT_ACTOR:
        return Directive(
            False, "(task-scoped)", "noop",
            f"task-reset is a user decision: --by must name the customer, not "
            f"{AGENT_ACTOR!r}",
            marker=DIRECTIVE_ESCALATE_TO_USER,
            data={"task": args.task, "by": by},
        )
    task_accumulator.reset(args.task)
    task_accumulator.clear_since_user_decision(args.task, now=_utcnow())
    return Directive(
        True, "(task-scoped)", "noop",
        f"cross-session task accumulator reset for task {args.task!r} by {by}: {args.reason}",
        data={"task": args.task, "reason": args.reason, "by": by},
    )


def cmd_mandate_grant(args, *, store: StateStore, runner: Runner | None = None) -> Directive:
    """Create a standing mandate: the user's one-time grant of bounded authority to the
    background debt cycle. Refused for `--by agent`, and for an id that already has a
    record (extend or resume that one instead)."""
    refusal = _mandate_by_refusal(args)
    if refusal:
        return refusal
    if mandate_store.load_mandate(args.mandate_id) is not None:
        return Directive(
            False, _MANDATE_NODE, "noop",
            f"mandate {args.mandate_id!r} already exists; use mandate-extend or mandate-resume",
            data={"mandate": args.mandate_id},
        )
    defaults = mandate_rules.MandateLimits()
    limits = mandate_rules.MandateLimits(**{
        name: (getattr(args, name) if getattr(args, name) is not None else getattr(defaults, name))
        for name in mandate_rules.LIMIT_FIELDS
    })
    now = mandate_store.utcnow()
    mandate = mandate_rules.new_mandate(
        args.mandate_id, args.by.strip(), now, limits,
        term_days=args.days if args.days is not None else mandate_rules.DEFAULT_TERM_DAYS,
    )
    mandate_store.save_mandate(mandate)
    mandate_store.append_event(
        mandate.id, "granted", {"expires_at": mandate.expires_at}, by=mandate.granted_by, now=now,
    )
    return Directive(
        True, _MANDATE_NODE, "noop",
        f"mandate {mandate.id!r} granted by {mandate.granted_by} until {mandate.expires_at}",
        data=_mandate_view(mandate, now),
    )


def cmd_mandate_extend(args, *, store: StateStore, runner: Runner | None = None) -> Directive:
    refusal = _mandate_by_refusal(args)
    if refusal:
        return refusal
    mandate = mandate_store.load_mandate(args.mandate_id)
    if mandate is None:
        return Directive(False, _MANDATE_NODE, "noop", f"no mandate {args.mandate_id!r}")
    now = mandate_store.utcnow()
    mandate = mandate_rules.extended(mandate, now, args.days)
    mandate_store.save_mandate(mandate)
    mandate_store.append_event(
        mandate.id, "extended", {"expires_at": mandate.expires_at}, by=args.by.strip(), now=now,
    )
    return Directive(
        True, _MANDATE_NODE, "noop",
        f"mandate {mandate.id!r} extended to {mandate.expires_at}",
        data=_mandate_view(mandate, now),
    )


def cmd_mandate_resume(args, *, store: StateStore, runner: Runner | None = None) -> Directive:
    """Unpause a stopped mandate and close an open breaker -- a user decision, since the
    breaker opens exactly when the cycle's own judgement failed."""
    refusal = _mandate_by_refusal(args)
    if refusal:
        return refusal
    if mandate_store.load_mandate(args.mandate_id) is None:
        return Directive(False, _MANDATE_NODE, "noop", f"no mandate {args.mandate_id!r}")
    detail = mandate_store.resume_cycle(args.mandate_id, by=args.by.strip(), runner=runner)
    now = mandate_store.utcnow()
    return Directive(
        True, _MANDATE_NODE, "noop",
        f"mandate {args.mandate_id!r} resumed by {args.by.strip()}",
        data={**_mandate_view(mandate_store.load_mandate(args.mandate_id), now), "resume": detail},
    )


def cmd_mandate_retry(args, *, store: StateStore, runner: Runner | None = None) -> Directive:
    """Let the cycle take an issue again after an item-local failure -- a user decision,
    since the cycle excludes a failed issue until the user says otherwise."""
    refusal = _mandate_by_refusal(args)
    if refusal:
        return refusal
    if mandate_store.load_mandate(args.mandate_id) is None:
        return Directive(False, _MANDATE_NODE, "noop", f"no mandate {args.mandate_id!r}")
    mandate_store.allow_retry(args.mandate_id, args.issue, by=args.by.strip())
    return Directive(
        True, _MANDATE_NODE, "noop",
        f"mandate {args.mandate_id!r}: issue #{args.issue} may be taken again, allowed by {args.by.strip()}",
        data={"mandate": args.mandate_id, "issue": args.issue},
    )


def cmd_mandate_stop(args, *, store: StateStore, runner: Runner | None = None) -> Directive:
    """Pause the mandate, disable its timer and kill a running cycle. Open to the agent:
    stopping only ever narrows authority."""
    if not mandate_store.mandate_exists(args.mandate_id):
        return Directive(False, _MANDATE_NODE, "noop", f"no mandate {args.mandate_id!r}")
    detail = mandate_store.stop_cycle(args.mandate_id, runner=runner)
    if detail["killed"]:
        outcome = f"killed cycle pid {detail['killed']['pid']}"
    elif detail["cycle_running"]:
        outcome = "a cycle holds the lock but its pid is unknown, nothing killed"
    else:
        outcome = "no running cycle"
    try:
        view = _mandate_view(mandate_store.load_mandate(args.mandate_id), mandate_store.utcnow())
    except (mandate_rules.MandateError, AttributeError):
        view = {"mandate": None, "record_unreadable": True}
    return Directive(
        True, _MANDATE_NODE, "noop",
        f"mandate {args.mandate_id!r} stopped; {outcome}",
        data={**view, "stop": detail},
    )


def cmd_mandate_status(args, *, store: StateStore, runner: Runner | None = None) -> Directive:
    mandate = mandate_store.load_mandate(args.mandate_id)
    if mandate is None:
        return Directive(False, _MANDATE_NODE, "noop", f"no mandate {args.mandate_id!r}")
    now = mandate_store.utcnow()
    view = _mandate_view(mandate, now)
    return Directive(
        True, _MANDATE_NODE, "noop",
        f"mandate {mandate.id!r}: " + ("may start a cycle" if view["may_start"] else "refused: " + ", ".join(view["refusals"])),
        data=view,
    )


def register(sub, add):
    sp = add("task-reset", help="explicit renegotiation: zero the cross-session "
             "task accumulator (item B) for --task — never called from `reset`")
    # Session-independent by design (cmd_task_reset's own docstring), but
    # _inject_default_session unconditionally appends --session <harness> when
    # $CLAUDE_CODE_SESSION_ID is set (i.e. every real invocation inside a
    # Claude Code session) — mirror plan-render's suppressed-absorb pattern so
    # that injection doesn't crash argparse with "unrecognized arguments".
    sp.add_argument("--session", required=False, default=None, help=argparse.SUPPRESS)
    sp.add_argument("--task", required=True, help="task_id whose accumulator to zero")
    sp.add_argument("--reason", required=True,
                    help="why this task's accumulated cross-session friction is being forgiven")
    sp.add_argument("--by", required=True,
                    help="the customer authorizing the reset; the coordinator is refused")

    def add_mandate(name: str, help_: str, *, by: bool = False) -> argparse.ArgumentParser:
        sub = add(name, help=help_)
        sub.add_argument("--session", required=False, default=None, help=argparse.SUPPRESS)
        sub.add_argument("--id", dest="mandate_id", default=mandate_rules.DEFAULT_MANDATE_ID,
                         help="mandate slug (default: %(default)s)")
        if by:
            sub.add_argument("--by", required=True,
                             help="the customer authorizing this; the coordinator is refused")
        return sub

    sp = add_mandate("mandate-grant", "create the standing mandate for the background debt cycle", by=True)
    sp.add_argument("--days", type=float, default=None, help="term length in days")
    for limit, unit in (
        ("daily_usd", "USD"), ("weekly_usd", "USD"), ("item_usd_cap", "USD"),
        ("item_minutes_cap", "minutes"), ("cycle_minutes_cap", "minutes"),
        ("veto_window_hours", "hours"),
    ):
        sp.add_argument(f"--{limit.replace('_', '-')}", dest=limit, type=float, default=None,
                        help=f"override the default ({unit})")
    sp.add_argument("--max-items", dest="max_items", type=int, default=None,
                    help="items taken per cycle")
    sp = add_mandate("mandate-extend", "push the mandate's expiry out", by=True)
    sp.add_argument("--days", type=float, required=True, help="days to add past the later of now and expiry")
    add_mandate("mandate-resume", "unpause the mandate and close its breaker", by=True)
    sp = add_mandate("mandate-retry", "let the cycle take an issue again after an item-local failure", by=True)
    sp.add_argument("--issue", type=int, required=True, help="the issue number to allow again")
    add_mandate("mandate-stop", "pause the mandate, disable its timer, kill a running cycle")
    add_mandate("mandate-status", "show the mandate, its spend windows and whether a cycle may start")
