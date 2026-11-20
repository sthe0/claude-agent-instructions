"""Command line of the OpenCode probe suite (``scripts/opencode-probe.py`` is a shim)."""
from __future__ import annotations

import argparse
import sys

from . import core, coverage, harness, negative, record, runner, stub, userconfig


def _cmd_user_config(args) -> int:
    if args.show is not None:
        return userconfig.show_dependency_file(args.show)
    if args.list:
        return userconfig.list_top_level()
    if args.snapshot:
        return userconfig.write_snapshot()
    return userconfig.compare()


def _case_ids(args) -> set[str] | None:
    return set(args.cases.split(",")) if args.cases else None


def _cmd_run(args) -> int:
    problem = core.binary_problem()
    if problem:
        print(f"SKIP: {problem}", file=sys.stderr)
        return 2 if args.require_live else 3
    codes = []
    for phase in core.select_phases(args.phase):
        rec_dir, cases = runner.run_phase(phase, case_ids=_case_ids(args))
        k7 = {c.case_id: c.k7 for c in core.phase_cases(phase)}
        print(f"{phase.name}: record {rec_dir}")
        for case_id, entry in sorted(cases.items()):
            print(f"  {case_id}: {entry['status']}")
        codes.append(record.run_exit_code({cid: e["status"] for cid, e in cases.items()}, k7))
    return record.worst_exit(codes)


def _cmd_check_record(args) -> int:
    problems = []
    for phase in core.select_phases(args.phase):
        problems += record.check_phase(core.state_dir(), phase, require_pass=args.require_pass,
                                       case_ids=_case_ids(args))
    for problem in problems:
        print(f"check-record: {problem}", file=sys.stderr)
    return 1 if problems else 0


def _cmd_pin(args) -> int:
    problems = record.pin_record(core.state_dir(), core.get_phase(args.phase))
    for problem in problems:
        print(f"pin: {problem}", file=sys.stderr)
    return 1 if problems else 0


def _cmd_negative(args) -> int:
    problem = core.binary_problem()
    if problem:
        print(f"SKIP: {problem}", file=sys.stderr)
        return 0
    return negative.run_negative(core.get_phase(args.phase), _case_ids(args))


def _cmd_subject_coverage(args) -> int:
    uncovered = coverage.uncovered(args.since, args.until)
    for rel in uncovered:
        print(f"subject-coverage: {rel} is in no phase's SUBJECT_FILES and not in SUBJECT_ONLY")
    return 1 if uncovered else 0


def _cmd_stub(args) -> int:
    return stub.serve_forever(args.scenario, args.port_file)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="opencode-probe")
    sub = parser.add_subparsers(dest="command", required=True)
    p = sub.add_parser("run", help="run the cases of a phase against the pinned binary")
    p.add_argument("--phase", required=True, help="phase name or 'all'")
    p.add_argument("--cases", help="comma-separated case ids")
    p.add_argument("--require-live", action="store_true", help="a missing binary is an error, not a skip")
    p.set_defaults(func=_cmd_run)
    p = sub.add_parser("check-record", help="check the latest record of the current tree")
    p.add_argument("--phase", required=True)
    p.add_argument("--require-pass", action="store_true")
    p.add_argument("--cases")
    p.set_defaults(func=_cmd_check_record)
    p = sub.add_parser("pin", help="write last_pass of a phase from its latest fully accepted record")
    p.add_argument("--record", action="store_true", required=True)
    p.add_argument("--phase", required=True)
    p.set_defaults(func=_cmd_pin)
    p = sub.add_parser("negative", help="run a phase with the plugin's refusals inert")
    p.add_argument("--phase", required=True)
    p.add_argument("--inert-deny", action="store_true", required=True)
    p.add_argument("--cases")
    p.set_defaults(func=_cmd_negative)
    p = sub.add_parser("subject-coverage", help="changed subject files must be covered by a phase")
    p.add_argument("--since", required=True)
    p.add_argument("--until", required=True)
    p.set_defaults(func=_cmd_subject_coverage)
    p = sub.add_parser("stub", help="serve a stub scenario until interrupted")
    p.add_argument("--scenario", required=True)
    p.add_argument("--port-file", required=True)
    p.set_defaults(func=_cmd_stub)
    p = sub.add_parser("user-config", help="snapshot or compare the user's OpenCode config")
    group = p.add_mutually_exclusive_group(required=True)
    group.add_argument("--snapshot", action="store_true")
    group.add_argument("--compare", action="store_true")
    group.add_argument("--list", action="store_true", help="top-level entries (read-only)")
    group.add_argument("--show", metavar="REL",
                       help="print a file under ~/.opencode/node_modules (read-only)")
    p.set_defaults(func=_cmd_user_config)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        return args.func(args)
    except (core.InternalError, harness.VersionMismatch) as exc:
        print(f"opencode-probe: internal error: {exc}", file=sys.stderr)
        return 2
