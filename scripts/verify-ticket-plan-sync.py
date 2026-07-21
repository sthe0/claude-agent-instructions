#!/usr/bin/env python3
"""Tracker-agnostic comparator: does a ticket's posted plan comment still
match the current machine-readable TOML plan?

Difficulty (functional ground):
  A plan posted as a human-readable ticket comment and the machine-readable
  TOML plan it was rendered from can silently drift — the TOML gets edited
  (replan, refinement) without the comment being re-posted, and a session can
  even continue work on a ticket without ever confirming a plan is attached
  at all. "Does the posted plan still match?" is a state-DECIDABLE question
  (byte-identity of the plan the comment claims to reflect), so it should be
  a comparator, not something the coordinator re-derives from memory on every
  continuation.

Mechanism:
  The posted comment embeds a single marker line:

      <!-- agent-plan-sync: plan_sha256=<hex> plan=<basename> -->

  ``plan_sha256`` is the sha256 of the plan file's bytes at the moment it was
  posted — the SAME definition `scripts/agentctl/cli.py::_plan_file_sha256`
  already uses to bind a plan-review verdict or a delivery stamp to an exact
  plan version. This script does not invent a new hash; it reuses that
  notion so "does the posted plan match" and "is this plan-review verdict
  still valid" answer from one shared definition.

  This script is intentionally tracker-agnostic: it never fetches a comment
  itself. The caller (a project-specific tracker transport, kept out of this
  PUBLIC repo) extracts the last-posted comment's text and passes it in via
  --comment-file/--marker; this script only computes and compares hashes.

Modes:
  --emit-marker --plan PATH   Print the marker line to embed when posting
                               PATH as a ticket comment. Exit 0.
  --plan PATH (--comment-file FILE|- | --marker STRING)
                               Compare PATH's current hash against the
                               marker found in the given text. Prints one of:
                                 OK       — hashes match, comment is current
                                 DRIFT    — marker present, hash differs
                                 NO-PLAN  — no marker found in the given text
                               Exit 0 for OK, 1 for DRIFT/NO-PLAN.
  --selftest                   Exercise the OK/DRIFT/NO-PLAN paths against
                               fabricated temp files. Exit 0 iff all pass.
"""
from __future__ import annotations

import argparse
import hashlib
import re
import sys
import tempfile
from pathlib import Path

MARKER_RE = re.compile(
    r"<!--\s*agent-plan-sync:\s*plan_sha256=(?P<sha>[0-9a-f]{64})\s+"
    r"plan=(?P<name>\S+)\s*-->"
)


def plan_file_sha256(path: str) -> str:
    """sha256 of a plan file's bytes. Mirrors agentctl/cli.py::_plan_file_sha256
    (same definition, so a marker embedded via that binding compares cleanly
    here) but raises OSError on an unreadable path instead of degrading to ''
    — an unreadable --plan is a usage error for this standalone check, not a
    best-effort default."""
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def format_marker(plan_path: str, plan_sha256: str | None = None) -> str:
    """The exact marker line to embed when posting plan_path as a comment."""
    sha = plan_sha256 if plan_sha256 is not None else plan_file_sha256(plan_path)
    return f"<!-- agent-plan-sync: plan_sha256={sha} plan={Path(plan_path).name} -->"


def extract_marker(text: str) -> "dict | None":
    """The last marker in text (a comment may be edited/appended-to; the last
    one wins), or None when no marker is present."""
    matches = list(MARKER_RE.finditer(text or ""))
    if not matches:
        return None
    m = matches[-1]
    return {"plan_sha256": m.group("sha"), "plan": m.group("name")}


def check(current_sha256: str, marker: "dict | None") -> str:
    """OK / DRIFT / NO-PLAN — see module docstring."""
    if marker is None:
        return "NO-PLAN"
    if marker["plan_sha256"] == current_sha256:
        return "OK"
    return "DRIFT"


def _read_comment_text(comment_file: "str | None", marker_arg: "str | None") -> str:
    if marker_arg is not None:
        return marker_arg
    if comment_file == "-":
        return sys.stdin.read()
    return Path(comment_file).read_text(encoding="utf-8", errors="replace")


def _selftest() -> bool:
    ok_all = True
    with tempfile.TemporaryDirectory() as tmp:
        plan_path = Path(tmp) / "plan.toml"
        plan_path.write_text("[meta]\ntask_id = \"t\"\n", encoding="utf-8")
        current_sha = plan_file_sha256(str(plan_path))

        # OK: marker emitted for this exact plan, round-tripped through extract.
        posted = f"Plan posted.\n{format_marker(str(plan_path), current_sha)}\n"
        status = check(current_sha, extract_marker(posted))
        print(f"selftest OK case: {status}")
        ok_all = ok_all and status == "OK"

        # DRIFT: marker names a different hash (plan was edited after posting).
        stale_sha = "0" * 64 if current_sha != "0" * 64 else "1" * 64
        posted_stale = f"Plan posted.\n{format_marker(str(plan_path), stale_sha)}\n"
        status = check(current_sha, extract_marker(posted_stale))
        print(f"selftest DRIFT case: {status}")
        ok_all = ok_all and status == "DRIFT"

        # NO-PLAN: no marker in the comment text at all.
        status = check(current_sha, extract_marker("No plan comment posted yet."))
        print(f"selftest NO-PLAN case: {status}")
        ok_all = ok_all and status == "NO-PLAN"

    return ok_all


def main(argv: "list[str] | None" = None) -> int:
    parser = argparse.ArgumentParser(
        description=(
            "Compare a ticket's posted plan-sync marker against the current "
            "TOML plan's sha256 (tracker-agnostic; the caller supplies the "
            "posted comment text)."
        )
    )
    parser.add_argument("--plan", help="path to the TOML plan file")
    parser.add_argument("--comment-file", help="file with the posted comment text, or - for stdin")
    parser.add_argument("--marker", help="the marker line/text directly, instead of --comment-file")
    parser.add_argument("--emit-marker", action="store_true", help="print the marker line for --plan and exit")
    parser.add_argument("--selftest", action="store_true", help="run the OK/DRIFT/NO-PLAN self-test and exit")
    args = parser.parse_args(argv)

    if args.selftest:
        return 0 if _selftest() else 1

    if not args.plan:
        parser.error("--plan is required (unless --selftest)")

    if args.emit_marker:
        print(format_marker(args.plan))
        return 0

    if not args.comment_file and args.marker is None:
        parser.error("one of --comment-file or --marker is required (unless --emit-marker/--selftest)")

    try:
        current_sha = plan_file_sha256(args.plan)
    except OSError as exc:
        print(f"verify-ticket-plan-sync: cannot read plan {args.plan!r}: {exc}")
        return 2

    try:
        text = _read_comment_text(args.comment_file, args.marker)
    except OSError as exc:
        print(f"verify-ticket-plan-sync: cannot read comment {args.comment_file!r}: {exc}")
        return 2

    marker = extract_marker(text)
    status = check(current_sha, marker)

    if status == "OK":
        print(f"verify-ticket-plan-sync: OK — posted plan matches {args.plan} (plan_sha256={current_sha})")
        return 0
    if status == "DRIFT":
        print(
            f"verify-ticket-plan-sync: DRIFT — posted marker plan_sha256="
            f"{marker['plan_sha256']} != current {current_sha} for {args.plan}. "
            "Re-post the plan (this is a plan actualization, not silent divergence)."
        )
        return 1
    print(
        f"verify-ticket-plan-sync: NO-PLAN — no agent-plan-sync marker found in the "
        f"given comment text for {args.plan}. Confirm a plan is actually attached "
        "to the ticket, then post one with its marker."
    )
    return 1


if __name__ == "__main__":
    sys.exit(main())
