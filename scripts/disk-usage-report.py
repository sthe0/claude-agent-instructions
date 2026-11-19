#!/usr/bin/env python3
"""Read-only report of what holds a filesystem: entries by size, what each is, whether a reaper reclaims it.

Walks one filesystem at idle I/O and CPU priority within a deadline (at most 60 minutes), never
enters a mount point, never follows a symlink, deletes nothing. Writes disk-usage-<date>[-n].json
and .md into --out-dir. Naming rows come from a neutral default table in Core merged under a
machine table at <reaper plugin dir>/disk-report/names.toml (or --names PATH). Contract and the
reaper-status rule: docs/operations/reapers.md (section "Disk-usage report").

Example: python3 scripts/disk-usage-report.py --out-dir ~/.local/state/disk-guard/part2c
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from lib import disk_report  # noqa: E402


def main(argv: "list[str] | None" = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--root", default="/", help="filesystem root to walk (default /)")
    parser.add_argument("--names", help="machine naming table (default: <plugin dir>/disk-report/names.toml)")
    parser.add_argument("--out-dir", required=True, help="directory receiving the dated .json and .md")
    parser.add_argument("--deadline-min", type=float, default=disk_report.MAX_DEADLINE_MIN,
                        help="wall-clock bound in minutes (default and maximum 60)")
    parser.add_argument("--min-bytes", type=int, default=disk_report.DEFAULT_MIN_BYTES,
                        help="unnamed directories below this fold into one entry per parent (default 2^30)")
    parser.add_argument("--max-depth", type=int, default=disk_report.DEFAULT_MAX_DEPTH, help="du depth below each root (default 4)")
    parser.add_argument("--temp-root", action="append", help="temp root to total (repeatable; default /tmp and the session temp root)")
    parser.add_argument("--home", default=str(Path.home()), help="home directory (never a du root; default $HOME)")
    parser.add_argument("--mountinfo", default="/proc/self/mountinfo", help="mountinfo file (default /proc/self/mountinfo)")
    args = parser.parse_args(argv)

    names = [disk_report.DEFAULT_TABLE, Path(args.names) if args.names else disk_report.machine_table_path()]
    try:
        report = disk_report.build_report(
            root=args.root, home=args.home, names_paths=names, mountinfo=args.mountinfo,
            deadline_min=args.deadline_min, min_bytes=args.min_bytes, max_depth=args.max_depth,
            temp_roots=args.temp_root, argv=[str(Path(__file__).resolve()), *(argv if argv is not None else sys.argv[1:])],
        )
        json_path, md_path = disk_report.write_outputs(report, Path(args.out_dir))
    except disk_report.ReportError as exc:
        print(f"disk-usage-report: {exc}", file=sys.stderr)
        return 2
    unknown = report["unknown"]
    print(f"{json_path}\n{md_path}\nbound (b)+(c) = {unknown['bound_pct']:.2f}% of Used (limit {unknown['bound_limit_pct']}%)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
