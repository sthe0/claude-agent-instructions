#!/usr/bin/env python3
"""What the engine's escape hatches are, and how often the delivery one is taken.

Difficulty removed: an escape hatch with no recorded reason silently becomes the
normal path. On this machine 3 of the first 5 delivery stamps were `override`,
and nothing said why — so a hatch designed for a dead hook had quietly become
the majority route to approval, and that fact was invisible for as long as the
reason went unrecorded. A free-text `note` cannot close this: notes are an
archive, not a count. This report turns the typed `escape_reason` into the
count, and lists the OTHER hatches so the same blind spot is not re-created one
flag at a time.

Two halves, deliberately different in kind:

  1. USAGE — parse every delivery sidecar under the state dir(s) and group by
     source and escape_reason. Measured, not declared.

  2. INVENTORY — the engine's other escape-hatch arguments, DECLARED in
     _ESCAPE_HATCH_ARGS below. Not derived by scanning argparse for
     force/waiver/override/escape: a name-pattern scan is a heuristic, and
     presenting a heuristic as an inventory would repeat, at a smaller scale,
     precisely the blind spot this task exists to remove. The scan is instead
     the REVERSE-direction test (tests/test_escape_hatch_report.py), which goes
     red on a hatch-shaped argument nobody registered here — the same shape
     lib/hook_wiring.py's gate-bearing registry uses.

Known non-argument hatches, listed here rather than in the structure because
the structure's contract is "every entry is a live parser argument":
`plan-review --verdict override` (and the same value on stage-review /
code-review) is an escape expressed as a VALUE of a choices= argument, so no
argument name carries it. It is counted by neither half today.

Read-only: opens sidecars and the parser, writes nothing.
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from agentctl import delivery  # noqa: E402
from lib import config_root  # noqa: E402

# (subcommand, dest, what it lets the operator past, what records WHY it was taken)
_ESCAPE_HATCH_ARGS: "tuple[tuple[str, str, str, str], ...]" = (
    (
        "confirm-delivery", "escape_reason",
        "the delivery-stamp half of the plan-presentation gate",
        "typed: one of " + "/".join(delivery.DELIVERY_ESCAPE_REASONS) +
        ", plus a free-text --note",
    ),
    (
        "reset", "force",
        "the refusal to discard an in-flight task's state",
        "nothing — the discarded state is gone and no reason is recorded",
    ),
    (
        "replan", "coverage_waiver",
        "the check that a corrected plan still covers the original difficulty",
        "free text only — countable as present/absent, not by kind",
    ),
    (
        "replan", "normalization_waiver",
        "the requirement to re-norm a reproducible factor at difficulty closure",
        "free text only — countable as present/absent, not by kind",
    ),
)


def stamp_files(state_dirs: "list[Path]") -> "list[Path]":
    """Every delivery sidecar under the given state dirs, sorted for stable
    output. A missing dir is silence, not an error: a machine that has never run
    a substantive task has no state dir and still deserves a report."""
    out: "list[Path]" = []
    for d in state_dirs:
        if not d.is_dir():
            continue
        out.extend(sorted(d.glob("*.delivery.json")))
    return out


def load_stamps(paths: "list[Path]") -> "tuple[list, list[Path]]":
    """Parse each sidecar into a DeliveryStamp, collecting the unreadable ones
    separately. Reported rather than dropped: a sidecar this reader cannot parse
    is an escape that happened and went uncounted, which is the failure this
    report exists to make impossible."""
    stamps = []
    unreadable: "list[Path]" = []
    for p in paths:
        try:
            data = json.loads(p.read_text(encoding="utf-8"))
            stamps.append(delivery.DeliveryStamp.from_dict(data))
        except Exception:
            unreadable.append(p)
    return stamps, unreadable


def tally(stamps: "list") -> "dict[str, dict[str, int]]":
    """Counts of stamps by source, then by escape_reason within each source.

    A stamp with no reason is counted under "" rather than skipped — the stamps
    written before the field existed are exactly the population that motivated
    it, and hiding them would flatter the report.
    """
    out: "dict[str, dict[str, int]]" = {}
    for s in stamps:
        by_reason = out.setdefault(s.source, {})
        reason = (s.escape_reason or "").strip()
        by_reason[reason] = by_reason.get(reason, 0) + 1
    return out


def format_usage(counts: "dict[str, dict[str, int]]", total: int,
                 unreadable: "list[Path]") -> "list[str]":
    lines = [f"delivery stamps: {total}"]
    if not total:
        lines.append("  (none — no substantive plan has been presented yet)")
    for source in sorted(counts):
        source_total = sum(counts[source].values())
        lines.append(f"  {source}: {source_total}")
        for reason in sorted(counts[source]):
            # An empty reason means two different things and must not read as
            # one: on a hook stamp it is correct (the hook path never takes an
            # escape and is barred from setting the field), on an override it
            # means the stamp predates the typed field.
            if reason:
                label = reason
            elif source == delivery.SOURCE_HOOK:
                label = "(not an escape — automated verification)"
            else:
                label = "(no reason recorded — predates --escape-reason)"
            lines.append(f"    {label}: {counts[source][reason]}")
    for p in unreadable:
        lines.append(f"  UNREADABLE (an uncounted escape): {p}")
    return lines


def format_inventory() -> "list[str]":
    lines = ["escape hatches in the engine:"]
    for command, dest, past, recorded in _ESCAPE_HATCH_ARGS:
        flag = "--" + dest.replace("_", "-")
        lines.append(f"  {command} {flag}")
        lines.append(f"    gets past: {past}")
        lines.append(f"    reason recorded: {recorded}")
    return lines


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    p.add_argument("--state-dir", dest="state_dir", default=None,
                   help="state directory to scan (default: the current and "
                        "legacy agentctl state dirs)")
    return p


def main(argv: "list[str] | None" = None) -> int:
    args = build_parser().parse_args(argv)
    if args.state_dir:
        dirs = [Path(args.state_dir).expanduser()]
    else:
        dirs = [config_root.agentctl_state_dir(), config_root.agentctl_legacy_state_dir()]
    stamps, unreadable = load_stamps(stamp_files(dirs))
    lines = format_usage(tally(stamps), len(stamps), unreadable)
    lines.append("")
    lines.extend(format_inventory())
    print("\n".join(lines))
    return 0


if __name__ == "__main__":
    sys.exit(main())
