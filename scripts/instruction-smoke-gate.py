#!/usr/bin/env python3
"""Pre-land instruction smoke gate: run the isolated-root smoke test, record it, check a landing.

  instruction-smoke-gate.py run [--ref HEAD] [--waive-unavailable REASON] [-C DIR]
      Fetch the remote tip, build the sandbox of the candidate commit, run the verify recipe
      (one live `claude -p` launch) and write the record
      <git common dir>/instruction-smoke/<sha>.json. Exit 0 admitted (PASS, or UNAVAILABLE under
      a waiver), 1 refused (FAIL, or UNAVAILABLE without a waiver), 2 usage or environment.
      The candidate must contain the remote tip: the record is bound to it.

  instruction-smoke-gate.py check [--ref HEAD] [-C DIR]
      Fetch the remote tip and report whether landing --ref would be admitted. Exit 0/1/2.

  instruction-smoke-gate.py waive --ref SHA --reason TEXT [-C DIR]
      Attach a waiver to an existing UNAVAILABLE record. Only a record whose sole non-PASS
      checks are live:* and UNAVAILABLE takes one; a FAIL or a static/canon check never does.

  instruction-smoke-gate.py pre-push <remote-name> <remote-url>
      The git pre-push hook entry (stdin: `<local-ref> <local-sha> <remote-ref> <remote-sha>`).
      Exit 0 admits the push, 1 refuses it. Reads local state only; never fetches or runs.
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from lib import instruction_smoke_gate as gate  # noqa: E402


def _out(text: str) -> None:
    print(gate.defuse(text))


def _repo(args: argparse.Namespace) -> str:
    return args.directory or "."


def _print_record(outcome: gate.RunOutcome) -> None:
    record = outcome.record
    _out(f"smoke: candidate {gate.short(record['candidate_sha'])} on base {gate.short(record['base_sha'])}: "
         f"{record['result']}")
    for check in record["checks"]:
        _out(f"  {check['status']:<11} {check['name']}  {check['detail']}".rstrip())
    _out(f"smoke: record {outcome.record_path}")
    if outcome.sandbox_root is not None:
        _out(f"smoke: sandbox kept at {outcome.sandbox_root}")
    if outcome.waiver_note:
        _out(f"smoke: {outcome.waiver_note}")


def cmd_run(args: argparse.Namespace) -> int:
    outcome = gate.run_smoke(
        _repo(args), ref=args.ref, remote=args.remote, trunk=args.trunk,
        timeout_s=args.timeout, waiver=args.waive_unavailable,
    )
    _print_record(outcome)
    if outcome.admitted:
        _out(f"smoke: ADMITTED ({gate.admission_summary(outcome.record)})")
        if outcome.record["result"] == gate.UNAVAILABLE:
            _out("smoke: WAIVER IN EFFECT: the live launch did not complete; the recorded reason is "
                 "the only evidence the instructions load")
        return 0
    _out(f"smoke: REFUSED: {outcome.reason}")
    return 1


def cmd_check(args: argparse.Namespace) -> int:
    repo = _repo(args)
    candidate = gate.resolve_commit(repo, args.ref)
    base = gate.fetch_remote_tip(repo, args.remote, args.trunk)
    decision = gate.evaluate_landing(repo, candidate, base)
    verdict = "ADMITTED" if decision.allowed else "REFUSED"
    _out(f"check: {verdict} ({decision.kind}): {decision.reason}")
    return 0 if decision.allowed else 1


def cmd_waive(args: argparse.Namespace) -> int:
    repo = _repo(args)
    sha = gate.resolve_commit(repo, args.ref)
    path, record = gate.waive_record(repo, sha, args.reason)
    _out(f"waive: recorded on {gate.short(sha)}: {gate.admission_summary(record)}")
    _out(f"waive: record {path}")
    return 0


def cmd_pre_push(args: argparse.Namespace) -> int:
    try:
        result = gate.prepush_decision(sys.stdin.read().splitlines(), ".", trunk=args.trunk)
    except Exception as exc:  # noqa: BLE001 — a hook that crashes must still refuse, in defused words
        result = gate.PrePushResult(False, (gate.defuse(f"pre-push: refused: gate error: {gate.one_line(exc, 200)}"),))
    for message in result.messages:
        print(message, file=sys.stderr)
    return 0 if result.allowed else 1


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="instruction-smoke-gate.py", description=__doc__.split("\n")[0])
    sub = parser.add_subparsers(dest="command", required=True)

    def common(p: argparse.ArgumentParser, ref: bool = True) -> None:
        p.add_argument("-C", dest="directory", default=None, help="repository (default: cwd)")
        p.add_argument("--remote", default=gate.DEFAULT_REMOTE)
        p.add_argument("--trunk", default=gate.DEFAULT_TRUNK)
        if ref:
            p.add_argument("--ref", default="HEAD", help="candidate commit (default: HEAD)")

    run = sub.add_parser("run", help="smoke the candidate and write its record")
    common(run)
    run.add_argument("--timeout", type=int, default=gate.DEFAULT_TIMEOUT_S, help="seconds per live launch")
    run.add_argument("--waive-unavailable", metavar="REASON", default=None,
                     help="admit an UNAVAILABLE live launch, recording REASON")
    run.set_defaults(func=cmd_run)

    check = sub.add_parser("check", help="would landing --ref be admitted?")
    common(check)
    check.set_defaults(func=cmd_check)

    waive = sub.add_parser("waive", help="waive an UNAVAILABLE record")
    common(waive)
    waive.add_argument("--reason", required=True)
    waive.set_defaults(func=cmd_waive)

    pre_push = sub.add_parser("pre-push", help="git pre-push hook entry")
    pre_push.add_argument("--trunk", default=gate.DEFAULT_TRUNK)
    pre_push.add_argument("remote_name", nargs="?")
    pre_push.add_argument("remote_url", nargs="?")
    pre_push.set_defaults(func=cmd_pre_push)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        return args.func(args)
    except gate.GateError as exc:
        _out(f"instruction-smoke-gate: {exc}")
        return 1 if exc.refused else 2


if __name__ == "__main__":
    sys.exit(main())
