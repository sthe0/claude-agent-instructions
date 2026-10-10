#!/usr/bin/env python3
"""Pre-land instruction smoke gate: run the isolated-root smoke test, record it, check a landing.

  instruction-smoke-gate.py run [--ref HEAD] [--waiver REASON] [-C DIR]
      Fetch the remote tip, build the sandbox of the candidate commit, run the verify recipe
      (one live `claude -p` launch) and write the record
      <git common dir>/instruction-smoke/<sha>.json. Exit 0 admitted (PASS, or UNAVAILABLE under
      a waiver), 1 refused (FAIL, or UNAVAILABLE without a waiver), 2 usage or environment.
      The candidate must contain the remote tip: the record is bound to it. The last stdout
      line is `RECORD: <path>` whenever a record was written.

  instruction-smoke-gate.py check [--sha HEAD] [--remote-sha SHA] [--record PATH] [-C DIR]
      Report whether landing --sha onto a remote tip of --remote-sha would be admitted, reading
      the record from PATH (default: the record store). With --remote-sha nothing is fetched;
      without it the remote tip is fetched first. Exit 0 admitted, 1 refused, 2 usage or environment.

  instruction-smoke-gate.py waive --sha SHA --reason TEXT [-C DIR]
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


def _repo(args: argparse.Namespace) -> str:
    return args.directory or "."


def _print_record(outcome: gate.RunOutcome) -> None:
    record = outcome.record
    print(f"smoke: candidate {gate.short(record['candidate_sha'])} on base {gate.short(record['base_sha'])}: "
          f"{record['result']}")
    for check in record["checks"]:
        print(f"  {check['status']:<11} {check['name']}  {check['detail']}".rstrip())
    if outcome.sandbox_root is not None:
        print(f"smoke: sandbox kept at {outcome.sandbox_root}")
    if outcome.waiver_note:
        print(f"smoke: {outcome.waiver_note}")


def cmd_run(args: argparse.Namespace) -> int:
    outcome = gate.run_smoke(
        _repo(args), ref=args.ref, remote=args.remote, trunk=args.trunk,
        timeout_s=args.timeout, waiver=args.waiver,
    )
    _print_record(outcome)
    if outcome.admitted:
        print(f"smoke: ADMITTED ({gate.admission_summary(outcome.record)})")
        if outcome.record["result"] == gate.UNAVAILABLE:
            print("smoke: WAIVER IN EFFECT: the live launch did not complete; the recorded reason is "
                  "the only evidence the instructions load")
    else:
        print(gate.defuse(f"smoke: REFUSED: {outcome.reason}"))
    print(f"RECORD: {outcome.record_path}")
    return 0 if outcome.admitted else 1


def cmd_check(args: argparse.Namespace) -> int:
    repo = _repo(args)
    candidate = gate.resolve_commit(repo, args.sha)
    if args.remote_sha is not None:
        remote_sha = gate.resolve_commit(repo, args.remote_sha)
    else:
        remote_sha = gate.fetch_remote_tip(repo, args.remote, args.trunk)
    decision = gate.evaluate_landing(repo, candidate, remote_sha, record_file=args.record)
    if decision.allowed:
        print(f"check: ADMITTED ({decision.kind}): {decision.reason}")
    else:
        print(gate.defuse(f"check: REFUSED ({decision.kind}): {decision.reason}"))
    return 0 if decision.allowed else 1


def cmd_waive(args: argparse.Namespace) -> int:
    repo = _repo(args)
    sha = gate.resolve_commit(repo, args.sha)
    path, record = gate.waive_record(repo, sha, args.reason)
    print(f"waive: recorded on {gate.short(sha)}: {gate.admission_summary(record)}")
    print(f"waive: record {path}")
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

    def common(p: argparse.ArgumentParser) -> None:
        p.add_argument("-C", dest="directory", default=None, help="repository (default: cwd)")
        p.add_argument("--remote", default=gate.DEFAULT_REMOTE)
        p.add_argument("--trunk", default=gate.DEFAULT_TRUNK)

    run = sub.add_parser("run", help="smoke the candidate and write its record")
    common(run)
    run.add_argument("--ref", default="HEAD", help="candidate commit (default: HEAD)")
    run.add_argument("--timeout", type=int, default=gate.DEFAULT_TIMEOUT_S, help="seconds per live launch")
    run.add_argument("--waiver", metavar="REASON", default=None,
                     help="admit an UNAVAILABLE live launch, recording REASON")
    run.set_defaults(func=cmd_run)

    check = sub.add_parser("check", help="would landing --sha be admitted?")
    common(check)
    check.add_argument("--sha", default="HEAD", help="candidate commit (default: HEAD)")
    check.add_argument("--remote-sha", default=None,
                       help="remote tip to bind to; given, nothing is fetched (default: fetch the tip)")
    check.add_argument("--record", metavar="PATH", default=None,
                       help="record file to evaluate (default: the record store)")
    check.set_defaults(func=cmd_check)

    waive = sub.add_parser("waive", help="waive an UNAVAILABLE record")
    common(waive)
    waive.add_argument("--sha", required=True, help="candidate commit whose record is waived")
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
        text = f"instruction-smoke-gate: {exc}"
        print(gate.defuse(text) if exc.refused else text)
        return 1 if exc.refused else 2


if __name__ == "__main__":
    sys.exit(main())
