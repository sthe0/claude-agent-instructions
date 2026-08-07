#!/usr/bin/env python3
"""Prove, from the ledger, that a judge hook really dispatched under the NEW
wiring — in this session, after this change.

Difficulty removed: "the hooks are re-registered with a bigger timeout" is a
claim about a config file, and a config file is not evidence that anything
ran. The old registration killed each hook below its own judge's fastest
measured call, so the failure mode being fixed is precisely one that leaves
no trace: exit 0, no output, no error. This script demands a positive trace
instead — a ledger line that could not have existed under the old limit.

Read-only. It opens the ledger and a snapshot file, and nothing else — no
judge is called, no subprocess is spawned, no session state is written.

FAIL-CLOSED throughout. Anything it cannot determine — a missing snapshot, a
hook whose old registration could not be read, a record with no timestamp —
counts as NOT witnessed, never as "probably fine". A witness that relaxes
under uncertainty witnesses nothing.

A record counts as evidence only if all three filters pass:

  1. it is newer than the --since-file stamp's mtime (so a line written
     before the re-wiring cannot vouch for the re-wiring);
  2. its ``source`` equals --session-id (so another session's line, or an
     older run of this one, cannot vouch either);
  3. its ``source`` is not a manual-run tag — a hand-driven or test
     invocation is not the harness dispatching the hook.

Then, per hook, judged against THAT hook's own old limit from the snapshot:

  * old registration ABSENT and the snapshot states that absence was
    established over the FULL settings scope — the hook was not registered at
    all before, so any in-window line of its own is already new evidence;
  * old registration ABSENT but the absence is QUALIFIED (the usual case: the
    probe could not read project-level settings) — fail closed, because "not
    registered in the members we could read" does not license the conclusion
    "any execution is new";
  * old registration WIRED with a numeric timeout — a call must be recorded
    lasting LONGER than that timeout, since the old harness would have killed
    the process first;
  * anything else (unknown status, wired with no timeout key, hook missing
    from the snapshot, unrecognised status string) — fail closed.

The snapshot is written and validated by lib/dispatch_witness_snapshot.py,
which owns the schema so the capture step and this reader cannot drift apart;
its module docstring carries the shape and the reason for each field.

The live session id comes from exactly one of two places, and never from a
default: --session-id names it outright, --session-from-env reads it from the
harness's own SESSION_ID_ENV. The env form exists because the caller that
matters — a verification command run inside the session being witnessed —
cannot type an id it does not know. An unset or blank env var is a failure,
not an empty filter: matching every record would turn the session filter off
precisely when it could not be applied.

Usage:
    scripts/check-dispatch-witness.py --since-file STAMP \\
        (--session-id ID | --session-from-env) \\
        --old-wiring-file SNAPSHOT [--ledger PATH] [--require-all]

Exit status: 0 when the witness holds, 1 when it does not.
"""
from __future__ import annotations

import argparse
import os
import sys
from dataclasses import dataclass
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from lib import dispatch_witness_snapshot  # noqa: E402
from lib import hook_wiring  # noqa: E402
from lib import judge_latency  # noqa: E402
from lib import judge_ledger  # noqa: E402

SNAPSHOT_SCHEMA = dispatch_witness_snapshot.SNAPSHOT_SCHEMA
load_snapshot = dispatch_witness_snapshot.load_snapshot

# The harness exports the live session id under this name, and only this one.
# scripts/spawn-specialist.py, hooks/hook-scope-track.py and
# agentctl/edit_ledger.py all read the same variable; a near-miss spelling was
# a real defect once (tests/test_spawn_specialist_lineage.py records that the
# pre-fix code read a non-existent name and silently logged null), which is
# why it is a named constant here rather than a string literal inline.
SESSION_ID_ENV = "CLAUDE_CODE_SESSION_ID"

# lib/judge_ledger.py writes "manual" when a payload carries no session_id and
# "unknown" before any payload is parsed. Neither is a harness dispatch, and
# both are rejected even if --session-id were passed one of these strings.
MANUAL_SOURCE_TAGS = frozenset({"manual", "unknown"})

# The hooks a witness is expected to cover. Taken from the existing
# single source of truth for the judge-calling hook set rather than retyped.
WITNESSED_BASENAMES = tuple(sorted(judge_latency.HOOK_CALL_SEQUENCE))


@dataclass(frozen=True)
class HookVerdict:
    """One hook's outcome. ``blocking`` separates the two failure kinds: a
    hook that simply stayed silent fails only --require-all, while a hook we
    could not reason about at all fails the run in either mode — the silence
    is data, the unknown is not."""

    basename: str
    witnessed: bool
    blocking: bool
    detail: str


def stamp_cutoff(path: Path) -> "tuple[float | None, str]":
    try:
        return path.stat().st_mtime, ""
    except OSError as exc:
        return None, f"--since-file stamp unreadable: {exc}"


def in_window(record: dict, cutoff: float, session_id: str) -> bool:
    """The three evidence filters, applied together."""
    ts = record.get("ts")
    if isinstance(ts, bool) or not isinstance(ts, (int, float)):
        return False
    if ts <= cutoff:
        return False
    source = record.get("source")
    if not isinstance(source, str) or source in MANUAL_SOURCE_TAGS:
        return False
    return source == session_id


def evidence_for(records: "list[dict]", hook_name: str, cutoff: float, session_id: str):
    """Every in-window record belonging to one hook, by the ledger's own hook
    name (not the script basename)."""
    return [
        record
        for record in records
        if record.get("hook") == hook_name and in_window(record, cutoff, session_id)
    ]


def longest_call(records: "list[dict]") -> "float | None":
    """The longest measured call duration among these records, or None."""
    durations = [
        float(record["duration"])
        for record in records
        if not isinstance(record.get("duration"), bool)
        and isinstance(record.get("duration"), (int, float))
    ]
    return max(durations) if durations else None


def judge_hook(
    basename: str, entry, records: "list[dict]", cutoff: float, session_id: str
) -> HookVerdict:
    # Unreachable while WITNESSED_BASENAMES and HOOK_NAME_BY_BASENAME are both
    # derived from the same hook set — and kept anyway, because it is the only
    # thing standing between a future fourth hook added to one table but not
    # the other and a witness quietly filtering records on `hook == None`,
    # which matches nothing and reads as "silent". Indexing the map directly
    # instead would turn that drift into a KeyError crash mid-verification.
    hook_name = judge_ledger.HOOK_NAME_BY_BASENAME.get(basename)
    if hook_name is None:
        return HookVerdict(
            basename, False, True,
            "no ledger hook name is mapped for this script "
            "(lib/judge_ledger.HOOK_NAME_BY_BASENAME)",
        )
    if not isinstance(entry, dict):
        return HookVerdict(
            basename, False, True, "absent from the old-wiring snapshot"
        )
    status = entry.get("status")
    if status == hook_wiring.UNKNOWN:
        return HookVerdict(
            basename, False, True,
            "old registration could not be determined — nothing can be concluded",
        )
    if status not in (hook_wiring.WIRED, hook_wiring.ABSENT):
        return HookVerdict(
            basename, False, True, f"unrecognised snapshot status {status!r}"
        )

    evidence = evidence_for(records, hook_name, cutoff, session_id)

    if status == hook_wiring.ABSENT:
        if entry.get("scope_qualified"):
            members = entry.get("members_read")
            read = len(members) if isinstance(members, list) else 0
            return HookVerdict(
                basename, False, True,
                f"the old registration was absent only from the {read} settings "
                f"members the capture step could read — project-level settings "
                f"were not among them, so 'was never wired' is not established "
                f"and no line of its own can be called new evidence",
            )
        if evidence:
            return HookVerdict(
                basename, True, False,
                f"was not registered before, over the full settings scope; "
                f"{len(evidence)} in-window ledger records show the harness "
                f"dispatching it under the new wiring — dispatch only; nothing "
                f"here shows it outlived its new limit",
            )
        return HookVerdict(
            basename, False, False, "was not registered before, and has not run yet"
        )

    old_timeout = entry.get("timeout")
    if isinstance(old_timeout, bool) or not isinstance(old_timeout, (int, float)):
        return HookVerdict(
            basename, False, True,
            "registered before, but with no readable timeout — the old limit to "
            "beat is unknown",
        )
    longest = longest_call(evidence)
    if longest is None:
        return HookVerdict(
            basename, False, False,
            f"no in-window call recorded; nothing beats the old {old_timeout}s limit",
        )
    if longest > old_timeout:
        return HookVerdict(
            basename, True, False,
            f"a {longest:.2f}s call outlived the old {old_timeout}s limit",
        )
    return HookVerdict(
        basename, False, False,
        f"longest in-window call {longest:.2f}s does not exceed the old "
        f"{old_timeout}s limit — the old wiring could have produced it too",
    )


def check(
    records: "list[dict]",
    hooks: dict,
    cutoff: float,
    session_id: str,
    *,
    require_all: bool,
) -> "tuple[bool, list[str]]":
    verdicts = [
        judge_hook(basename, hooks.get(basename), records, cutoff, session_id)
        for basename in WITNESSED_BASENAMES
    ]
    lines = []
    for verdict in verdicts:
        mark = "WITNESSED" if verdict.witnessed else ("UNKNOWN" if verdict.blocking else "silent")
        lines.append(f"  [{mark:>10}] {verdict.basename}: {verdict.detail}")
    # The writer emits exactly the hooks it was given, and the capture step is
    # given WITNESSED_BASENAMES — so a name outside that set means the snapshot
    # and this script disagree about which hooks are being witnessed. Ignoring
    # it silently is how "3 of 3 witnessed" gets printed for a snapshot that
    # describes a fourth hook nobody looked at.
    unexpected = sorted(set(hooks) - set(WITNESSED_BASENAMES))
    if unexpected:
        lines.append(
            f"  [{'UNKNOWN':>10}] snapshot describes hooks this script does not "
            f"witness ({', '.join(unexpected)}) — the two disagree about the "
            f"hook set, so neither can be trusted about coverage"
        )
    if unexpected or any(v.blocking for v in verdicts):
        lines.append("Result: FAILED — at least one hook could not be reasoned about.")
        return False, lines
    witnessed = [v for v in verdicts if v.witnessed]
    if require_all and len(witnessed) != len(verdicts):
        lines.append(
            f"Result: FAILED — --require-all demands every hook; "
            f"{len(witnessed)} of {len(verdicts)} are witnessed."
        )
        return False, lines
    if not witnessed:
        lines.append("Result: FAILED — no hook produced a witness.")
        return False, lines
    lines.append(f"Result: OK — {len(witnessed)} of {len(verdicts)} hooks witnessed.")
    return True, lines


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--since-file", type=Path, required=True,
                        help="stamp file whose mtime is the earliest admissible record")
    # Exactly one, and required: an id supplied by neither route would leave
    # the session filter with nothing to compare against, and a witness that
    # drops a filter it cannot apply is not a witness.
    session = parser.add_mutually_exclusive_group(required=True)
    session.add_argument("--session-id",
                         help="the live session id a record's `source` must equal")
    session.add_argument("--session-from-env", action="store_true",
                         help=f"take that session id from ${SESSION_ID_ENV}")
    parser.add_argument("--old-wiring-file", type=Path, required=True,
                        help=f"snapshot of the OLD registrations ({SNAPSHOT_SCHEMA})")
    parser.add_argument("--ledger", type=Path, default=None,
                        help="ledger path (default: the configured judge execution ledger)")
    parser.add_argument("--require-all", action="store_true",
                        help="fail unless every judge hook is witnessed")
    return parser


def resolve_session_id(args) -> "tuple[str | None, str]":
    """The one session id every record is filtered against, or (None, reason).

    Fails closed on a blank env var: an empty filter would match every record
    exactly when the id could not be determined, which is the permissive
    reading of an unknown this script exists to refuse."""
    if not args.session_from_env:
        return args.session_id, ""
    value = os.environ.get(SESSION_ID_ENV, "").strip()
    if not value:
        return None, (
            f"--session-from-env was given but ${SESSION_ID_ENV} is unset or blank; "
            f"there is no session to filter on"
        )
    return value, ""


def main(argv: "list[str] | None" = None) -> int:
    args = build_parser().parse_args(argv)
    lines = []
    cutoff, stamp_error = stamp_cutoff(args.since_file)
    hooks, snapshot_error = load_snapshot(args.old_wiring_file)
    session_id, session_error = resolve_session_id(args)
    for error in (stamp_error, snapshot_error, session_error):
        if error:
            lines.append(f"  {error}")
    if cutoff is None or hooks is None or session_id is None:
        lines.append("Result: FAILED — the witness cannot be evaluated.")
        print("\n".join(lines))
        return 1
    # read_ledger resolves None to the configured ledger itself, so the
    # default needs no separate resolution here. Its error and dropped-line
    # counts are reported rather than swallowed: the verdict below would fail
    # closed either way, but "no hook produced a witness" and "the ledger could
    # not be read" call for different next steps.
    read = judge_ledger.read_ledger(args.ledger)
    if read.error and not read.missing:
        lines.append(f"  ledger could not be read: {read.error}")
    if read.dropped_lines:
        lines.append(
            f"  {read.dropped_lines} malformed ledger lines were skipped; a "
            f"witness may have been among them"
        )
    ok, verdict_lines = check(
        read.records, hooks, cutoff, session_id, require_all=args.require_all
    )
    print("\n".join(lines + verdict_lines))
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
