#!/usr/bin/env python3
"""Standing check: every script that can write files is classified in
scripts/canon_writers.toml.

Difficulty removed: a script that edits files in the read-only canonical
checkout leaves an uncommitted change there (issue #353). The fix is a route
(lib/worktree_route.py), but "every writer is routed or exempt for a stated
reason" is a claim over the whole scripts tree; recalled once, it rots the
day someone adds a writer. This verifier turns it into a standing gate:

  enumerate  git ls-files of scripts/ and githooks/ (minus scripts/tests/),
             keeping the files whose text matches a write-primitive pattern
             -- the candidates;
  classify   scripts/canon_writers.toml holds one reviewed entry per file:
             disposition, category, reason (a per-file judgement stored as
             data; the pattern picks candidates, it never decides meaning);
  verify     a candidate without an entry, a dangling entry, a bad
             disposition/category, an empty reason, or a `routed` entry whose
             file does not import lib.worktree_route all exit 1.

Pattern with the same enumerate-classify-verify shape: crutch-inventory.py +
crutch_registry.toml + verify-semantic-gates.py.

Honest limits: the picker is lexical, so a write through a primitive it has no
pattern for is invisible (add the pattern; patterns are never removed), and a
`not-canon` label is a review judgement the verifier cannot check. Files
outside scripts/ and githooks/ (cursor/scripts, samples/) are not in the
domain.

Exit: 0 ok, 1 findings, 2 usage or read error.
"""
from __future__ import annotations

import argparse
import ast
import re
import subprocess
import sys
import tomllib
from collections import Counter
from pathlib import Path

CATEGORY_DISPOSITION = {
    "routed": "routed",
    "exempt-maintenance": "exempt",
    "exempt-landing": "exempt",
    "exempt-config-root": "exempt",
    "not-canon": "not-canon",
    "deferred": "deferred",
}
DISPOSITIONS = ("routed", "exempt", "not-canon", "deferred")

_SEP = r"""(?:\s+|["']\s*,\s*["'])"""

# Any scannable file. Add patterns; never remove one.
COMMON_PATTERNS: dict[str, re.Pattern[str]] = {
    "write_text": re.compile(r"\.write_text\("),
    "write_bytes": re.compile(r"\.write_bytes\("),
    "open-for-write": re.compile(
        r"""\bopen\((?:(?:[^()]|\([^()]*\))*?,\s*)?(?:mode\s*=\s*)?"""
        r"""["'][rwxabt+]*[wax+][rwxabt+]*["']"""),
    "json.dump": re.compile(r"\bjson\.dump\("),
    "os.replace": re.compile(r"\bos\.replace\("),
    "os.rename": re.compile(r"\bos\.rename\("),
    "shutil.copy": re.compile(r"\bshutil\.copy"),
    "shutil.move": re.compile(r"\bshutil\.move\("),
    "shutil.rmtree": re.compile(r"\bshutil\.rmtree\("),
    "unlink": re.compile(r"\.unlink\("),
    "sed -i": re.compile(r"\bsed\s+(?:-\w+\s+)*-\w*i"),
    "git commit": re.compile(rf"\bgit{_SEP}commit\b"),
    "git apply": re.compile(rf"\bgit{_SEP}apply\b"),
    "git checkout --": re.compile(rf"\bgit{_SEP}checkout{_SEP}--"),
    "git tree-mutating": re.compile(
        rf"\bgit{_SEP}(?:pull|merge|reset|rebase|cherry-pick|restore|clean|stash|mv|rm|switch)(?![-\w])"),
    "os.remove": re.compile(r"\bos\.(?:remove|makedirs|symlink)\("),
    "mkdir/touch": re.compile(r"\.(?:mkdir|touch|symlink_to)\("),
}
# Shell files (.sh, githooks/) only; Python's `>` and `>>` are operators.
TEE_PATTERN = re.compile(r"\btee\b")
REDIRECT_PATTERN = re.compile(r"(?<![-=<>|&])(?:\d?|&)>>?\s*(\S+)")
SHELL_SUFFIXES = {".sh", ".bash"}
PYTHON_SUFFIX = ".py"


def tracked_domain(root: Path) -> list[str]:
    proc = subprocess.run(
        ["git", "ls-files", "--", "scripts", "githooks"],
        cwd=str(root), capture_output=True, text=True,
    )
    if proc.returncode != 0:
        raise RuntimeError(f"git ls-files failed: {proc.stderr.strip()}")
    return sorted(
        p for p in proc.stdout.splitlines()
        if p and not p.startswith("scripts/tests/")
    )


def _is_shell(rel: str, text: str) -> bool:
    if Path(rel).suffix in SHELL_SUFFIXES or rel.startswith("githooks/"):
        return True
    first = text.split("\n", 1)[0]
    return first.startswith("#!") and bool(re.search(r"\b(ba|z|da)?sh\b", first))


def _is_scannable(rel: str, text: str) -> bool:
    return (Path(rel).suffix in SHELL_SUFFIXES | {PYTHON_SUFFIX}
            or rel.startswith("githooks/") or text.startswith("#!"))


def _redirect_writes_file(line: str) -> bool:
    for match in REDIRECT_PATTERN.finditer(line):
        target = match.group(1)
        if target.startswith("&") or target.strip("\"'") == "/dev/null":
            continue
        return True
    return False


def candidate_hits(rel: str, text: str) -> list[str]:
    """Names of the write-primitive patterns this file's text matches."""
    if not _is_scannable(rel, text):
        return []
    shell = _is_shell(rel, text)
    hits: set[str] = set()
    for line in text.splitlines():
        if line.lstrip().startswith("#"):
            continue
        for name, pattern in COMMON_PATTERNS.items():
            if pattern.search(line):
                hits.add(name)
        if shell:
            if TEE_PATTERN.search(line):
                hits.add("tee")
            if _redirect_writes_file(line):
                hits.add("redirect")
    return sorted(hits)


def imports_worktree_route(rel: str, text: str) -> bool:
    if Path(rel).suffix != PYTHON_SUFFIX:
        return "worktree_route" in text
    try:
        tree = ast.parse(text)
    except SyntaxError:
        return False
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom):
            if node.module == "lib" and any(a.name == "worktree_route" for a in node.names):
                return True
            if node.module in ("lib.worktree_route", "worktree_route"):
                return True
        elif isinstance(node, ast.Import):
            if any(a.name in ("lib.worktree_route", "worktree_route") for a in node.names):
                return True
    return False


def load_registry(path: Path) -> dict[str, dict]:
    with open(path, "rb") as fh:
        data = tomllib.load(fh)
    writers = data.get("writers")
    if not isinstance(writers, dict):
        raise ValueError(f"{path}: missing [writers] table")
    return writers


def check(root: Path, registry: dict[str, dict]) -> tuple[list[str], Counter]:
    problems: list[str] = []
    domain = tracked_domain(root)
    domain_set = set(domain)

    for rel in domain:
        if rel in registry:
            continue
        path = root / rel
        if not path.is_file():
            continue
        try:
            text = path.read_text(encoding="utf-8", errors="replace")
        except OSError as exc:
            raise RuntimeError(f"cannot read {rel}: {exc}") from exc
        hits = candidate_hits(rel, text)
        if hits:
            problems.append(
                f"{rel}: candidate writer (matches {', '.join(hits)}) has no entry "
                f"in canon_writers.toml")

    counts: Counter = Counter()
    for rel, entry in sorted(registry.items()):
        if rel not in domain_set or not (root / rel).is_file():
            problems.append(f"{rel}: entry names a file that is not a tracked "
                            f"scripts/ or githooks/ file (dangling)")
            continue
        if not isinstance(entry, dict):
            problems.append(f"{rel}: entry must be an inline table")
            continue
        disposition = entry.get("disposition")
        category = entry.get("category")
        reason = entry.get("reason")
        if disposition not in DISPOSITIONS:
            problems.append(f"{rel}: disposition {disposition!r} not in {list(DISPOSITIONS)}")
        if category not in CATEGORY_DISPOSITION:
            problems.append(f"{rel}: category {category!r} not in {sorted(CATEGORY_DISPOSITION)}")
        elif disposition in DISPOSITIONS and CATEGORY_DISPOSITION[category] != disposition:
            problems.append(f"{rel}: category {category!r} does not belong to "
                            f"disposition {disposition!r}")
        if not isinstance(reason, str) or not reason.strip():
            problems.append(f"{rel}: empty reason")
        if disposition == "routed":
            text = (root / rel).read_text(encoding="utf-8", errors="replace")
            if not imports_worktree_route(rel, text):
                problems.append(f"{rel}: routed entry does not import lib.worktree_route")
        if disposition in DISPOSITIONS:
            counts[disposition] += 1
    return problems, counts


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--registry", type=Path, default=None,
                        help="registry TOML (default: scripts/canon_writers.toml)")
    parser.add_argument("--root", type=Path, default=None,
                        help="repository root (default: the checkout holding this script)")
    parser.add_argument("--staged", action="store_true",
                        help="accepted for verify-all; the check is always whole-repo")
    parser.add_argument("--list-candidates", action="store_true",
                        help="print every candidate file with its matched patterns "
                             "(to classify a new writer) and exit")
    args = parser.parse_args(argv)

    root = args.root or Path(__file__).resolve().parent.parent
    if args.list_candidates:
        try:
            for rel in tracked_domain(root):
                text = (root / rel).read_text(encoding="utf-8", errors="replace")
                hits = candidate_hits(rel, text)
                if hits:
                    print(f"{rel}\t{','.join(hits)}")
        except (OSError, RuntimeError) as exc:
            print(f"verify-canon-writers: error: {exc}", file=sys.stderr)
            return 2
        return 0
    registry_path = args.registry or root / "scripts" / "canon_writers.toml"
    try:
        registry = load_registry(registry_path)
        problems, counts = check(root, registry)
    except (OSError, ValueError, RuntimeError, tomllib.TOMLDecodeError) as exc:
        print(f"verify-canon-writers: error: {exc}", file=sys.stderr)
        return 2

    if problems:
        for problem in problems:
            print(f"verify-canon-writers: FAIL: {problem}")
        print(f"verify-canon-writers: {len(problems)} problem(s)")
        return 1
    summary = ", ".join(f"{name}={counts.get(name, 0)}" for name in DISPOSITIONS)
    print(f"verify-canon-writers: OK -- {sum(counts.values())} entries ({summary})")
    return 0


if __name__ == "__main__":
    sys.exit(main())
