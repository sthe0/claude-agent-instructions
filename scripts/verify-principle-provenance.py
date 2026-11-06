#!/usr/bin/env python3
"""Verify the experience <-> regularity provenance links of the global memory corpus.

Every difficulty/v1 leaf and every case carries `generalized_into: [...]`; every
regularity's `induced_from` mirrors those links, names only existing leaves, has
an experience or case source or a `rule_derived:` reason, and its
`regularities/MEMORY.md` pointer lists the same sources. Every global principle
names existing regularities in `grounded_in: [regularity:<slug>, ...]`, has no
`induced_from`, and its `principles/MEMORY.md` pointer lists the same slugs. Cases
carry the four case sections, a well-formed `source_digest`, `origin_tier: project`
and a citing regularity. The rules live in `lib/principle_provenance.py`; the
project-side checks (orphan, stale and leaking cases) run through
`record-experience.py provenance --project-dir D --check`.

--project DIR: also check a project's memory (the repo dir or its memory dir) with the same
rules: its regularities are induced from its experience, its principles are grounded in
regularities of the project tier first, then global.

Every principle that names grounds carries a `grounding_judged` stamp equal to the digest of
its link as it stands; a leaf still on the retired `leaf/v1` schema, an unstamped link and a
stale stamp are findings. A stale `given` ground or a judge that was down at promote time
(`grounding_unjudged`) only warns.

--judge: also ask the grounding judge about every link now (working tree only; a live model
call; a judge that is down warns).

--staged: run only when a staged path lies under memory-global/leaves/ (any leaf),
then check the whole corpus as the git index holds it (a link has two sides, so
a one-file check misses the side that was not staged; an unstaged edit or an
untracked file is not part of the commit and is not read). Exit 1 on any failure.
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
    listed = subprocess.run(["git", "-C", str(root), "ls-files", "-s", "-z", "--", pp.LEAVES_REL.as_posix()],
                            capture_output=True, check=False).stdout
    entries = []  # (blob sha, path) of each merged .md entry
    for line in filter(None, listed.decode("utf-8").split("\0")):
        meta, _, rel = line.partition("\t")
        _mode, sha, stage = meta.split()
        if stage == "0" and rel.endswith(".md"):
            entries.append((sha, rel))
    batch = subprocess.run(["git", "-C", str(root), "cat-file", "--batch"],
                           input="".join(f"{sha}\n" for sha, _ in entries).encode(),
                           capture_output=True, check=False).stdout
    files, pos = {}, 0
    for _sha, rel in entries:
        header_end = batch.index(b"\n", pos)
        size = int(batch[pos:header_end].split()[2])
        files[root / rel] = batch[header_end + 1:header_end + 1 + size].decode("utf-8")
        pos = header_end + 1 + size + 1
    return pp.corpus_from_texts(root / pp.LEAVES_REL, files)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--root", type=Path, default=Path(__file__).resolve().parent.parent)
    parser.add_argument("--staged", action="store_true")
    parser.add_argument("--project", type=Path, metavar="DIR",
                        help="also check this project's memory (the repo dir or its memory dir)")
    parser.add_argument("--judge", action="store_true",
                        help="also ask the grounding judge about every link now (a live model call; "
                             "an unavailable judge warns and never fails)")
    args = parser.parse_args(argv)
    if args.project and args.staged:
        parser.error("--project checks the working tree; it cannot be combined with --staged")
    if args.judge and args.staged:
        parser.error("--judge is a live model call; it cannot be combined with --staged")

    project = pp.project_memory(args.project)
    if project is not None and not project.is_dir():
        parser.error(f"--project: no project memory directory at {project}")

    if args.staged and not pp.staged_triggers(_staged_paths(args.root)):
        print(f"{NAME}: OK — no staged leaf")
        return 0
    leaves = args.root / pp.LEAVES_REL
    if not leaves.is_dir():
        print(f"{NAME}: OK — no {pp.LEAVES_REL}/ tree")
        return 0
    if args.staged:
        corpus = _indexed_corpus(args.root)
        findings = pp.unaccounted(corpus) + pp.provenance_check_findings(corpus)
    else:
        findings = pp.verify_corpus(args.root, project)
        if args.judge:
            findings += pp.live_link_findings(
                pp.load_corpus(leaves, project), runner=pp.live_runner(), cache=pp.judge_cache_dir())
    failures = [f for f in findings if f.kind not in pp.WARNING_KINDS]
    warnings = [f for f in findings if f.kind in pp.WARNING_KINDS]
    print(f"{NAME}: {'FAIL' if failures else 'OK'}")
    for label, group in (("", failures), ("warning: ", warnings)):
        for f in group:
            shown = f.path.relative_to(args.root) if f.path.is_relative_to(args.root) else f.path
            print(f"  {label}{f.kind}: {shown} — {f.detail}")
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
