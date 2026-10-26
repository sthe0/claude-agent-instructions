#!/usr/bin/env python3
"""Pre-land suite gate: run the full test suite in an isolated root, subtract the tip's baseline, check a landing.

  suite-gate.py run [--ref HEAD] [--remote-sha SHA] [--timeout S] [--waiver REASON] [-C DIR]
      Fetch the remote tip (unless --remote-sha names it), build the sandbox of the candidate commit
      and run the whole suite in it. A failure the tip's own run also has does not count; a failure
      absent from it is rerun once and counts when it fails again. Writes the record
      <git common dir>/suite-gate/<sha>.json. Exit 0 admitted (PASS, exempt, not required, or
      UNAVAILABLE under a waiver), 1 refused (FAIL, or UNAVAILABLE without a waiver), 2 usage or
      environment. The candidate must contain the remote tip: the record is bound to it. The last
      stdout line is `RECORD: <path>` whenever a record was written. --timeout is per run
      (default: config.md suite-gate-timeout-s).

  suite-gate.py check [--sha HEAD] [--remote-sha SHA] [--record PATH] [-C DIR]
      Report whether landing --sha onto a remote tip of --remote-sha would be admitted, from the
      stored record. With --remote-sha nothing is fetched; without it the remote tip is fetched
      first. Never runs the suite. Exit 0 admitted, 1 refused, 2 usage or environment.

  suite-gate.py show [--sha HEAD] [-C DIR]
      Print the stored record's status line. Read-only; exit 0.

  suite-gate.py waive --sha SHA --reason TEXT [-C DIR]
      Attach a waiver to an existing UNAVAILABLE record. A FAIL record never takes one.

  suite-gate.py pre-push <remote-name> <remote-url>
      The git pre-push hook entry (stdin: `<local-ref> <local-sha> <remote-ref> <remote-sha>`).
      Exit 0 admits the push, 1 refuses it. Reads local state only; never fetches or runs.

A landing that changes only .md files under memory-global/ or docs/ is exempt; the exemption is
read from git's own path list and cannot be requested.
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path
from typing import Callable

sys.path.insert(0, str(Path(__file__).resolve().parent))

from lib import landing_gate as shared  # noqa: E402
from lib import suite_gate as gate  # noqa: E402


def _repo(args: argparse.Namespace) -> str:
    return args.directory or "."


def _tip(args: argparse.Namespace, repo: str) -> str:
    if args.remote_sha is not None:
        return shared.resolve_commit(repo, args.remote_sha)
    return shared.fetch_remote_tip(repo, args.remote, args.trunk)


def _print_record(record: dict) -> None:
    print(f"SUITE: candidate {gate.short(record['candidate_sha'])} on base {gate.short(record['base_sha'])}: "
          f"{record['status']}")
    print(f"  candidate failures {len(record['candidate_failures'])}, baseline failures "
          f"{len(record['baseline_failures'])} (baseline {record['baseline_cache']}), "
          f"new {len(record['new_failures'])}, unconfirmed {len(record['unconfirmed'])}; "
          f"suite {record['duration_s']}s, build {record['build_s']}s")
    for node in record["new_failures"]:
        print(f"  NEW FAILURE {gate.one_line(node, 300)}")
    if record["unavailable_reason"]:
        print(f"  unavailable: {gate.one_line(record['unavailable_reason'], 400)}")
    print(f"  log: {record['log']}")


def cmd_run(args: argparse.Namespace) -> int:
    repo = _repo(args)
    candidate = shared.resolve_commit(repo, args.ref)
    decision = gate.evaluate_landing(
        repo, candidate, _tip(args, repo), timeout_s=args.timeout, waiver=args.waiver, reuse=False,
        runner=args.runner, clock=args.clock, sandbox_parent=args.sandbox_parent,
    )
    if decision.record_path is not None:
        _print_record(shared.read_record(decision.record_path))
    print(decision.line)
    if decision.record_path is not None:
        print(f"RECORD: {decision.record_path}")
    return 0 if decision.admitted else 1


def cmd_check(args: argparse.Namespace) -> int:
    repo = _repo(args)
    candidate = shared.resolve_commit(repo, args.sha)
    decision = gate.landing_decision(repo, candidate, _tip(args, repo), record_file=args.record)
    print(decision.line)
    return 0 if decision.admitted else 1


def cmd_show(args: argparse.Namespace) -> int:
    repo = _repo(args)
    record = gate.candidate_record(shared.git_common_dir(repo), shared.resolve_commit(repo, args.sha))
    if record is None:
        print("SUITE: no record")
    else:
        print(gate.defuse(f"SUITE: {record.get('status')} for {gate.short(str(record.get('candidate_sha')))} "
                          f"on {gate.short(str(record.get('base_sha')))}, ran {record.get('ran_at')}"))
    return 0


def cmd_waive(args: argparse.Namespace) -> int:
    repo = _repo(args)
    sha = shared.resolve_commit(repo, args.sha)
    path, record = gate.waive_record(repo, sha, args.reason)
    print(gate.defuse(f"SUITE: waiver recorded on {gate.short(sha)}: {gate.admission_summary(record)}"))
    print(f"RECORD: {path}")
    return 0


def cmd_pre_push(args: argparse.Namespace) -> int:
    try:
        result = gate.prepush_decision(sys.stdin.read().splitlines(), ".", trunk=args.trunk)
    except Exception as exc:  # noqa: BLE001 — a hook that crashes must still refuse, in defused words
        result = shared.PrePushResult(False, (gate.defuse(f"pre-push: refused: gate error: {gate.one_line(exc, 200)}"),))
    for message in result.messages:
        print(message, file=sys.stderr)
    return 0 if result.allowed else 1


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="suite-gate.py", description=__doc__.split("\n")[0])
    sub = parser.add_subparsers(dest="command", required=True)

    def common(p: argparse.ArgumentParser) -> None:
        p.add_argument("-C", dest="directory", default=None, help="repository (default: cwd)")
        p.add_argument("--remote", default=gate.DEFAULT_REMOTE)
        p.add_argument("--trunk", default=gate.DEFAULT_TRUNK)

    run = sub.add_parser("run", help="run the suite on the candidate and write its record")
    common(run)
    run.add_argument("--ref", default="HEAD", help="candidate commit (default: HEAD)")
    run.add_argument("--remote-sha", default=None,
                     help="remote tip to bind to; given, nothing is fetched (default: fetch the tip)")
    run.add_argument("--timeout", type=int, default=None, metavar="S",
                     help=f"seconds per suite run (default: config.md {gate.CONFIG_KEY})")
    run.add_argument("--waiver", metavar="REASON", default=None,
                     help="admit an UNAVAILABLE run, recording REASON")
    run.set_defaults(func=cmd_run)

    check = sub.add_parser("check", help="would landing --sha be admitted?")
    common(check)
    check.add_argument("--sha", default="HEAD", help="candidate commit (default: HEAD)")
    check.add_argument("--remote-sha", default=None,
                       help="remote tip to bind to; given, nothing is fetched (default: fetch the tip)")
    check.add_argument("--record", metavar="PATH", default=None,
                       help="record file to evaluate (default: the record store)")
    check.set_defaults(func=cmd_check)

    show = sub.add_parser("show", help="print the stored record's status")
    show.add_argument("-C", dest="directory", default=None, help="repository (default: cwd)")
    show.add_argument("--sha", default="HEAD")
    show.set_defaults(func=cmd_show)

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


def main(argv: list[str] | None = None, *, runner: gate.Runner | None = None,
         sandbox_parent: str | Path | None = None, clock: Callable[[], str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    args.runner, args.sandbox_parent, args.clock = runner, sandbox_parent, clock
    try:
        return args.func(args)
    except shared.GateError as exc:
        text = f"suite-gate: {exc}"
        print(gate.defuse(text) if exc.refused else text)
        return 1 if exc.refused else 2


if __name__ == "__main__":
    sys.exit(main())
