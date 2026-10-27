#!/usr/bin/env python3
"""Validate the resource-grant replay fixture (`fixtures/resource_grant_replay/denials.jsonl`).

Difficulty removed: the fixture is the answer key that later stages (the
per-segment resolver, the opaque-code judge stub, the PreToolUse grant hook)
are tested against. A malformed case, a missing class, a mislabelled
`expect`, or a leaked user-home path would silently weaken or poison every
test that replays it. This checker is the one gate on the key's shape.

Usage: check_replay_fixture.py <fixture.jsonl | /dev/stdin>

Exit 0 and print per-class counts when every rule holds; exit 1 naming the
first failing rule (`FAIL [<rule>] ...`). The README.md beside the fixture
(or, when the input is not a regular file, the README in the canonical
fixture directory) is scanned by the same leak rule.

Stdlib only.
"""
from __future__ import annotations

import json
import os
import re
import sys
from collections import Counter

FIXTURE_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "fixtures", "resource_grant_replay")

# Placeholders every path in a case is rewritten to.
VENUE = "{VENUE}"
PLACEHOLDERS = ("{VENUE}", "{SCRATCH}", "{OUTSIDE}")

FIELD_TYPES = {
    "id": str,
    "class": str,
    "kind": str,
    "tool": str,
    "venue": str,
    "branch": str,
    "grants": list,
    "expect": str,
    "expected_effects": list,
    "opaque": bool,
    "source": str,
}
OPTIONAL_FIELDS = {"command", "path", "opaque_kind", "allowed_by", "resolver"}

TOOLS = {"Bash", "Read", "Edit", "Write"}
KINDS = {"developer", "tech-writer", "thinker", "planner", "code-reviewer"}
EXPECTS = {"allow", "deny", "none"}
OPAQUE_KINDS = {"python-c", "stdin-body", "script-file"}
ALLOWED_BY = {"allow-entry", "mode"}

# Op sets derived from scripts/agentctl/resources.py: FileResource.mode is
# read|write, VcsRefResource.op is push|land, plus `commit` (the local-ref
# effect the per-segment resolver adds for git commit/merge). The answer key
# lists only state-changing effects (reads need no grant), so a file entry
# is always `write`; `read` is rejected with its own rule name.
FILE_OPS = {"write"}
VCS_OPS = {"commit", "push", "land"}
PROTECTED_REFS = re.compile(r"^(main|master|release-.*)$")

# class -> (minimum count, required expect)
CLASS_RULES = {
    "prefix-mismatch": (3, "allow"),
    "git-C": (3, "allow"),
    "python-c": (3, "allow"),
    "python-stdin": (3, "allow"),
    "compound": (3, "allow"),
    "push-own-branch": (3, "allow"),
    "scratchpad-read": (3, "allow"),
    "uncovered-write": (6, "deny"),
    "allowed-today": (3, "none"),
    "edit-write-outside": (1, "none"),
}
MIN_DENY = 6

# Leak patterns: user-home paths in every spelling, and token-shaped strings.
LEAK_PATTERNS = [
    ("home-path", re.compile(r"/home/[^/\s\"']+")),
    ("home-path-encoded", re.compile(r"-home-[A-Za-z0-9_.]")),
    ("home-path-macos", re.compile(r"/Users/[^/\s\"']+")),
    ("home-path-root", re.compile(r"/root(/|\b)")),
    ("home-path-tilde", re.compile(r"(^|[\s\"'=(:])~[A-Za-z0-9_.-]*/")),
    ("home-path-var", re.compile(r"\$\{?HOME\b")),
    ("token-github", re.compile(r"\b(ghp|gho|ghu|ghs|ghr)_[A-Za-z0-9]{20,}|github_pat_[A-Za-z0-9_]{20,}")),
    ("token-sk", re.compile(r"\bsk-[A-Za-z0-9_-]{16,}")),
    ("token-slack", re.compile(r"\bxox[abprs]-[A-Za-z0-9-]{10,}")),
    ("token-aws", re.compile(r"\bAKIA[0-9A-Z]{16}\b")),
    ("token-oauth", re.compile(r"\b(y[0-3]_[A-Za-z0-9_-]{20,}|AQAAAA[A-Za-z0-9_-]{20,})")),
    ("token-jwt", re.compile(r"\beyJ[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}")),
    ("token-bearer", re.compile(r"\b(Bearer|OAuth)\s+[A-Za-z0-9._~+/=-]{16,}")),
    ("token-hex", re.compile(r"\b[0-9a-fA-F]{32,}\b")),
]
# A long base64 run is a token only when it mixes cases and digits; this keeps
# repo paths and snake-case names (slash-free runs of lowercase) out of it.
BASE64_RUN = re.compile(r"[A-Za-z0-9+_=-]{40,}")


def _looks_like_base64_token(run: str) -> bool:
    return any(ch.isupper() for ch in run) and any(ch.islower() for ch in run) and any(ch.isdigit() for ch in run)


class Fail(Exception):
    def __init__(self, rule: str, msg: str):
        super().__init__(f"FAIL [{rule}] {msg}")
        self.rule = rule


def scan_leaks(text: str, where: str) -> None:
    for name, pat in LEAK_PATTERNS:
        m = pat.search(text)
        if m:
            raise Fail("leak", f"{where}: {name} match {m.group(0)[:40]!r}")
    for m in BASE64_RUN.finditer(text):
        if _looks_like_base64_token(m.group(0)):
            raise Fail("leak", f"{where}: token-base64 match {m.group(0)[:40]!r}")


def check_effect(e: object, where: str) -> None:
    if not isinstance(e, dict):
        raise Fail("effect-shape", f"{where}: effect is not an object")
    kind = e.get("kind")
    if kind == "file":
        if set(e) != {"kind", "path", "op"}:
            raise Fail("effect-shape", f"{where}: file effect keys must be kind,path,op; got {sorted(e)}")
        if e["op"] == "read":
            raise Fail("effect-read-listed", f"{where}: reads need no grant and are never listed")
        if e["op"] not in FILE_OPS:
            raise Fail("effect-op", f"{where}: file op {e['op']!r} not in {sorted(FILE_OPS)}")
        if not isinstance(e["path"], str) or not e["path"].startswith(PLACEHOLDERS):
            raise Fail("effect-path", f"{where}: file path must start with a placeholder; got {e['path']!r}")
    elif kind == "vcs_ref":
        if set(e) != {"kind", "remote", "ref", "op"}:
            raise Fail("effect-shape", f"{where}: vcs_ref effect keys must be kind,remote,ref,op; got {sorted(e)}")
        if e["op"] not in VCS_OPS:
            raise Fail("effect-op", f"{where}: vcs_ref op {e['op']!r} not in {sorted(VCS_OPS)}")
        if not all(isinstance(e[k], str) and e[k] for k in ("remote", "ref")):
            raise Fail("effect-shape", f"{where}: remote and ref must be non-empty strings")
        if (e["op"] == "commit") != (e["remote"] == "."):
            raise Fail("effect-commit-form", f"{where}: commit uses remote '.', push/land never do")
    else:
        raise Fail("effect-kind", f"{where}: effect kind {kind!r} not in ['file', 'vcs_ref']")


def is_worktree_root(path: str) -> bool:
    """`{VENUE}` itself, or one segment under `{OUTSIDE}` (a foreign checkout)."""
    return path == VENUE or re.fullmatch(r"\{OUTSIDE\}/[^/]+", path) is not None


def covered(case: dict, e: dict) -> bool:
    """The implied stage resource set: a developer or tech-writer may write
    under the venue and commit to the venue branch; a developer may also push
    the venue branch to origin unless it is main/master/release-*. Read-only
    kinds may write nothing."""
    kind, branch = case["kind"], case["branch"]
    if kind not in ("developer", "tech-writer"):
        return False
    if e["kind"] == "file":
        return e["path"] == VENUE or e["path"].startswith(VENUE + "/")
    if e["op"] == "commit":
        return e["ref"] == branch
    if e["op"] == "push":
        return (kind == "developer" and e["remote"] == "origin" and e["ref"] == branch
                and not PROTECTED_REFS.match(branch))
    return False


def check_case(c: object, n: int) -> dict:
    where = f"line {n}"
    if not isinstance(c, dict):
        raise Fail("case-shape", f"{where}: not a JSON object")
    where = f"line {n} ({c.get('id')!r})"
    for field, typ in FIELD_TYPES.items():
        if field not in c:
            raise Fail("field-missing", f"{where}: missing {field!r}")
        if not isinstance(c[field], typ):
            raise Fail("field-type", f"{where}: {field!r} must be {typ.__name__}")
    extra = set(c) - set(FIELD_TYPES) - OPTIONAL_FIELDS
    if extra:
        raise Fail("field-unknown", f"{where}: unknown fields {sorted(extra)}")
    if not c["id"] or not c["source"].strip() or not c["branch"]:
        raise Fail("field-empty", f"{where}: id, branch and source must be non-empty")
    if c["class"] not in CLASS_RULES:
        raise Fail("class-unknown", f"{where}: class {c['class']!r} not in {sorted(CLASS_RULES)}")
    if c["kind"] not in KINDS:
        raise Fail("kind-unknown", f"{where}: kind {c['kind']!r} not in {sorted(KINDS)}")
    if c["tool"] not in TOOLS:
        raise Fail("tool-unknown", f"{where}: tool {c['tool']!r} not in {sorted(TOOLS)}")
    if c["venue"] != VENUE:
        raise Fail("venue", f"{where}: venue must be {VENUE!r}")
    if c["expect"] not in EXPECTS:
        raise Fail("expect-unknown", f"{where}: expect {c['expect']!r} not in {sorted(EXPECTS)}")
    if not all(isinstance(g, str) and g for g in c["grants"]):
        raise Fail("field-type", f"{where}: grants must be non-empty strings")
    if c["tool"] == "Bash":
        if not isinstance(c.get("command"), str) or not c["command"] or "path" in c:
            raise Fail("command-or-path", f"{where}: Bash cases carry a non-empty command and no path")
    else:
        if not isinstance(c.get("path"), str) or not c["path"].startswith(PLACEHOLDERS) or "command" in c:
            raise Fail("command-or-path", f"{where}: {c['tool']} cases carry a placeholder path and no command")
    if c["opaque"] != ("opaque_kind" in c):
        raise Fail("opaque-kind-iff", f"{where}: opaque_kind present iff opaque is true")
    if c["opaque"] and c["opaque_kind"] not in OPAQUE_KINDS:
        raise Fail("opaque-kind-unknown", f"{where}: opaque_kind {c['opaque_kind']!r} not in {sorted(OPAQUE_KINDS)}")
    if (c["class"] == "allowed-today") != ("allowed_by" in c):
        raise Fail("allowed-by-iff", f"{where}: allowed_by present iff class is allowed-today")
    if "allowed_by" in c and c["allowed_by"] not in ALLOWED_BY:
        raise Fail("allowed-by-unknown", f"{where}: allowed_by {c['allowed_by']!r} not in {sorted(ALLOWED_BY)}")

    if "resolver" in c and (c["resolver"] != "fall-through" or c["tool"] != "Bash" or c["opaque"]):
        raise Fail("resolver-unknown", f"{where}: resolver may only be 'fall-through' on a non-opaque Bash case")

    for i, e in enumerate(c["expected_effects"]):
        check_effect(e, f"{where} effect {i}")
    effects = c["expected_effects"]
    if any(e["kind"] == "vcs_ref" and e["op"] == "commit" for e in effects) and not any(
            e["kind"] == "file" and is_worktree_root(e["path"]) for e in effects):
        raise Fail("effect-commit-form", f"{where}: a commit effect needs a file write on the worktree root")

    # Class-specific invariants.
    cls, expect = c["class"], c["expect"]
    if expect != CLASS_RULES[cls][1]:
        raise Fail("class-expect", f"{where}: class {cls} requires expect {CLASS_RULES[cls][1]!r}")
    if cls == "python-c" and c.get("opaque_kind") != "python-c":
        raise Fail("class-opaque", f"{where}: python-c cases are opaque python-c")
    if cls == "python-stdin" and c.get("opaque_kind") != "stdin-body":
        raise Fail("class-opaque", f"{where}: python-stdin cases are opaque stdin-body")
    if cls == "git-C" and "git -C " not in c.get("command", ""):
        raise Fail("class-shape", f"{where}: git-C cases use git -C")
    if cls == "scratchpad-read" and (effects or "{SCRATCH}" not in c.get("command", c.get("path", ""))):
        raise Fail("class-shape", f"{where}: scratchpad-read reads {{SCRATCH}} and lists no effects")
    if cls == "edit-write-outside" and (c["tool"] not in ("Edit", "Write") or VENUE in c["path"]):
        raise Fail("class-shape", f"{where}: edit-write-outside is an Edit/Write outside the venue")
    if cls == "uncovered-write" and (c["tool"] != "Bash" or not effects):
        raise Fail("class-shape", f"{where}: uncovered-write is a Bash call with a listed write")

    # The answer key must agree with the implied resource set.
    if expect == "allow" and not all(covered(c, e) for e in effects):
        raise Fail("expect-coverage", f"{where}: expect allow but an effect is not covered")
    if expect == "deny" and all(covered(c, e) for e in effects):
        raise Fail("expect-coverage", f"{where}: expect deny but every effect is covered")
    return c


DENY_SHAPES = {
    "deny-redirect-outside": lambda c: not c["opaque"] and ">" in c["command"]
    and any(e["kind"] == "file" and e["path"].startswith("{OUTSIDE}") for e in c["expected_effects"]),
    "deny-python-c-outside": lambda c: c.get("opaque_kind") == "python-c"
    and any(e["kind"] == "file" and e["path"].startswith("{OUTSIDE}") for e in c["expected_effects"]),
    "deny-push-main": lambda c: any(e["kind"] == "vcs_ref" and e["op"] == "push" and PROTECTED_REFS.match(e["ref"])
                                    for e in c["expected_effects"]),
    "deny-push-other-branch": lambda c: any(e["kind"] == "vcs_ref" and e["op"] == "push" and e["ref"] != c["branch"]
                                            and not PROTECTED_REFS.match(e["ref"]) for e in c["expected_effects"]),
    "deny-foreign-commit": lambda c: "git -C {OUTSIDE}" in c["command"]
    and any(e["kind"] == "vcs_ref" and e["op"] == "commit" for e in c["expected_effects"]),
}


def check(lines: list[str], readme_text: str | None, readme_where: str) -> Counter:
    cases = []
    for n, raw in enumerate(lines, 1):
        if not raw.strip():
            raise Fail("blank-line", f"line {n}: blank line")
        scan_leaks(raw, f"line {n}")
        try:
            obj = json.loads(raw)
        except json.JSONDecodeError as exc:
            raise Fail("json", f"line {n}: {exc}") from None
        if raw.rstrip("\n") != json.dumps(obj):
            raise Fail("serialization", f"line {n}: not written with json.dumps default separators")
        cases.append(check_case(obj, n))
    if readme_text is not None:
        scan_leaks(readme_text, readme_where)

    ids = Counter(c["id"] for c in cases)
    dup = [i for i, k in ids.items() if k > 1]
    if dup:
        raise Fail("unique-id", f"duplicate ids {dup}")
    counts = Counter(c["class"] for c in cases)
    for cls, (minimum, _) in CLASS_RULES.items():
        if counts[cls] < minimum:
            raise Fail("class-minimum", f"class {cls}: {counts[cls]} < {minimum}")
    denies = [c for c in cases if c["expect"] == "deny"]
    if len(denies) < MIN_DENY:
        raise Fail("deny-minimum", f"{len(denies)} deny cases < {MIN_DENY}")
    for shape, pred in DENY_SHAPES.items():
        if not any(pred(c) for c in denies):
            raise Fail("deny-shape", f"no deny case of shape {shape}")
    return counts


def main(argv: list[str]) -> int:
    if len(argv) != 2:
        print("usage: check_replay_fixture.py <fixture.jsonl | /dev/stdin>", file=sys.stderr)
        return 2
    src = argv[1]
    with open(src, encoding="utf-8") as fh:
        lines = fh.read().splitlines()
    readme_dir = os.path.dirname(os.path.abspath(src)) if os.path.isfile(src) else FIXTURE_DIR
    readme = os.path.join(readme_dir, "README.md")
    readme_text = None
    if os.path.isfile(readme):
        with open(readme, encoding="utf-8") as fh:
            readme_text = fh.read()
    try:
        counts = check(lines, readme_text, "README.md")
    except Fail as exc:
        print(exc)
        return 1
    for cls in CLASS_RULES:
        print(f"{cls}: {counts[cls]}")
    print(f"total: {sum(counts.values())} cases OK")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
