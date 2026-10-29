"""Judge the links between prescriptions and the knowledge they rest on (agentctl.grounding_judge).

  grounding-judge.py table <table.toml> [--out verdicts.json] [--cache DIR] [--leaves DIR] [--project DIR]
                           [--checked-path PATH ...]
      The verdicts file a bulk-conversion table needs: one record per [[leaf]], [[stamp]] and [[rebase]]
      entry, each cached at <cache>/<digest>.json (default: the judge's own cache directory).
  grounding-judge.py link <link.json> [--cache DIR] [--checked-path PATH ...]
      One link, from {"prescription": {...}, "grounds": [...], "candidates": [...], "override": "..."}.
  grounding-judge.py corpus --leaves DIR [--project DIR] [--cache DIR] [--checked-path PATH ...]
      Every principle of a live corpus that names grounds, keyed <tier>:<slug>.
  grounding-judge.py stamp <verdicts.json> <tier:path>
      The frontmatter lines the entry earns (none for a defective or unavailable link; exit 1); no model call.

A judge that cannot be reached leaves its records `unavailable` with a warning and exits 0: planning is
never stopped by it, and an unavailable record is never stamped. Output is JSON on stdout (or --out).
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from agentctl import advisor, grounding, grounding_judge  # noqa: E402


def _corpus(args):
    if not args.leaves:
        return None
    return grounding.load_ground_corpus(Path(args.leaves), Path(args.project) if args.project else None)


def _emit(data, out) -> None:
    text = json.dumps(data, indent=2, sort_keys=True, ensure_ascii=False) + "\n"
    if out:
        Path(out).write_text(text, encoding="utf-8")
    else:
        sys.stdout.write(text)


def _warn_unavailable(records) -> None:
    for tag, record in records.items():
        if record.get("status") == "unavailable":
            print(f"{tag}: {record.get('warning')}", file=sys.stderr)


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="grounding-judge.py", description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = parser.add_subparsers(dest="command", required=True)
    for name in ("table", "link", "corpus"):
        p = sub.add_parser(name)
        p.add_argument("--cache", help="directory of cached verdicts (default: the judge's own)")
        p.add_argument("--checked-path", action="append", default=[], dest="checked_paths",
                       help="a path the citing change checked; a given regularity sourced there is current")
        if name == "table":
            p.add_argument("table")
            p.add_argument("--out")
        if name == "link":
            p.add_argument("link")
        if name != "link":
            p.add_argument("--leaves", help="the global leaves directory (a corpus; for `corpus` required)")
            p.add_argument("--project", help="a project memory directory")
    stamp = sub.add_parser("stamp")
    stamp.add_argument("verdicts")
    stamp.add_argument("tag")
    return parser


def main(argv=None, runner=None) -> int:
    args = _parser().parse_args(argv)
    if args.command == "stamp":
        records = json.loads(Path(args.verdicts).read_text(encoding="utf-8")).get("links", {})
        lines = grounding_judge.stamp_lines(records.get(args.tag) or {})
        if lines:
            print("\n".join(lines))
        return 0 if lines else 1
    runner = runner or advisor.subprocess_runner
    cache = Path(args.cache) if args.cache else None
    checked = tuple(args.checked_paths)
    if args.command == "table":
        verdicts = grounding_judge.judge_table(args.table, runner=runner, cache=cache, corpus=_corpus(args),
                                               checked_paths=checked)
        _warn_unavailable(verdicts["links"])
        _emit(verdicts, args.out)
    elif args.command == "link":
        spec = json.loads(Path(args.link).read_text(encoding="utf-8"))
        outcome = grounding_judge.judge_grounding_link(
            spec["prescription"], spec.get("grounds", []), spec.get("candidates", []), runner=runner,
            cache=cache, checked_paths=checked, override=spec.get("override"))
        _warn_unavailable({"link": outcome})
        _emit(outcome, None)
    else:
        corpus = _corpus(args)
        if corpus is None:
            print("corpus: --leaves is required", file=sys.stderr)
            return 2
        records = grounding_judge.judge_corpus(corpus, runner=runner, cache=cache, checked_paths=checked)
        _warn_unavailable(records)
        _emit(records, None)
    return 0


if __name__ == "__main__":
    sys.exit(main())
