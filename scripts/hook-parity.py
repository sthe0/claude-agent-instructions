#!/usr/bin/env python3
"""Check a layer's hook parity table against the hooks the layer wires.

  hook-parity.py check --layer core [--strict] [--table F]
  hook-parity.py check --layer-root DIR [--strict] [--table F]
  hook-parity.py probe-header           # probe_paths / probe_mechanisms of the pinned record, as TOML
  hook-parity.py start-notice ROOT...   # the lines an adapter shows at session start

Exit 0 when nothing is reported, 1 otherwise. Rules, row format and the strict rules S1-S4:
``scripts/lib/hook_parity.py``; how the Core table is made and refreshed:
``integrations/opencode/README.md``.
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from lib import hook_parity  # noqa: E402


def _check(args) -> int:
    if args.layer:
        result = hook_parity.check_layer(table_path=args.table, strict=args.strict)
    else:
        result = hook_parity.check_layer(args.layer_root, table_path=args.table, strict=args.strict)
    for finding in result.findings:
        print(finding.line())
    if not result.findings:
        print(f"hook-parity: layer {result.layer} ok" + (" (strict)" if args.strict else ""))
    return 1 if result.findings else 0


def _probe_header(_args) -> int:
    print(hook_parity.render_probe_header(*hook_parity.pinned_probe_tables()))
    return 0


def _start_notice(args) -> int:
    for line in hook_parity.start_notice(args.roots):
        print(line)
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = parser.add_subparsers(dest="command", required=True)
    check = sub.add_parser("check", help="check one layer's table against its wiring")
    which = check.add_mutually_exclusive_group(required=True)
    which.add_argument("--layer", choices=["core"], help="the Core layer: rows equal the installer's DESIRED list")
    which.add_argument("--layer-root", type=Path, help="a layer root holding hook-parity.toml beside its settings file")
    check.add_argument("--table", type=Path, help="table file to use instead of the layer's own")
    check.add_argument("--strict", action="store_true", help="add the strict rules S1-S4")
    check.set_defaults(func=_check)
    header = sub.add_parser("probe-header", help="print the probe tables of the pinned mechanisms record")
    header.set_defaults(func=_probe_header)
    notice = sub.add_parser("start-notice", help="print the session-start notice lines for the layer roots")
    notice.add_argument("roots", nargs="+", type=Path)
    notice.set_defaults(func=_start_notice)
    args = parser.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
