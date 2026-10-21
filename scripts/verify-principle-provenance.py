#!/usr/bin/env python3
"""Verify the experience <-> principle provenance links of the global memory corpus.

Every difficulty/v1 leaf and every case carries `generalized_into: [...]`; every
principle's `induced_from` mirrors those links, names only existing leaves, has
an experience or case source or a `rule_derived:` reason, and its
`principles/MEMORY.md` pointer lists the same sources. Cases carry the four
case sections, a well-formed `source_digest`, `origin_tier: project` and a citing
principle. The rules live in `lib/principle_provenance.py`; the project-side
checks (orphan, stale and leaking cases) run through
`record-experience.py provenance --project-dir D --check`.

--staged: run only when a staged path lies under experience/ or principles/,
then check the whole corpus as the git index holds it (a link has two sides, so
a one-file check misses the side that was not staged; an unstaged edit or an
untracked file is not part of the commit and is not read). Exit 1 on any finding.
"""
from __future__ import annotations

import argparse
import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from lib import principle_provenance as pp  # noqa: E402

NAME = "verify-principle-provenance"


def _staged_paths(root: Path) -> list[str]:
    out = subprocess.run(["git", "-C", str(root), "diff", "--cached", "--name-only"],
                         capture_output=True, text=True, check=False)
    return out.stdout.splitlines()


def _indexed_corpus(root: Path) -> pp.Corpus:
    """The corpus as the index holds it, so an unstaged edit or an untracked file is not read."""
    listed = subprocess.run(["git", "-C", str(root), "ls-files", "-z", "--", pp.LEAVES_REL.as_posix()],
                            capture_output=True, check=False).stdout
    files = {}
    for rel in filter(None, listed.decode("utf-8").split("\0")):
        if not rel.endswith(".md"):
            continue
        shown = subprocess.run(["git", "-C", str(root), "show", f":{rel}"],
                               capture_output=True, check=False)
        if shown.returncode == 0:
            files[root / rel] = shown.stdout.decode("utf-8")
    return pp.corpus_from_texts(root / pp.LEAVES_REL, files)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--root", type=Path, default=Path(__file__).resolve().parent.parent)
    parser.add_argument("--staged", action="store_true")
    args = parser.parse_args(argv)

    if args.staged and not pp.staged_triggers(_staged_paths(args.root)):
        print(f"{NAME}: OK — no staged experience or principle leaf")
        return 0
    leaves = args.root / pp.LEAVES_REL
    if not leaves.is_dir():
        print(f"{NAME}: OK — no {pp.LEAVES_REL}/ tree")
        return 0
    corpus = _indexed_corpus(args.root) if args.staged else pp.load_corpus(leaves)
    findings = pp.unaccounted(corpus)
    if not findings:
        print(f"{NAME}: OK")
        return 0
    print(f"{NAME}: FAIL")
    for f in findings:
        print(f"  {f.kind}: {f.path.relative_to(args.root)} — {f.detail}")
    return 1


if __name__ == "__main__":
    sys.exit(main())
