#!/usr/bin/env python3
"""Reusable resolver: assert every `--deselect` site in a plan names one and the
same set of node ids, each site individually guarding every node it deselects —
plus a REPORT-ONLY sweep of the plan's own prose for sentences that speak of the
tolerated-red set while carrying a cardinality word or a bare definite singular
reference to a member of the set.

Two halves, split along the structural/semantic seam
(memory-global/leaves/regex-not-for-semantic-classification.md):

  * STRUCTURAL — decidable, and BLOCKS. Every `verify_command` and every
    `final_check[*].command` containing `--deselect` is a SITE. A site
    DESELECTS the node ids named after each `--deselect` flag, in either the
    `--deselect <node>` or the `--deselect=<node>` spelling; an occurrence in
    neither spelling is itself a violation naming the site, never a silent
    skip — a site whose every occurrence went unread would otherwise deselect
    an unbounded set and report a clean, empty one. A node is GUARDED when
    the command contains a single-node pytest invocation naming that node —
    a node id, which carries `::`, not a directory — whose exit status is
    captured into a variable and compared to exactly 1, in a conjunct that
    reaches the deselecting run through `&&` alone, so that the node being
    red is what lets that run happen. The binding is through the same
    variable name the invocation assigned, so a bare "-eq 1" anywhere in the
    command cannot pass as a guard for every node it contains. A site
    deselecting a node it does not guard, guarding a node it does not
    deselect, or naming a node set that disagrees with another site's, is a
    violation.

  * SEMANTIC — a high-recall prefilter, never a judge. It selects sentences
    carrying exception-set vocabulary together with EITHER a cardinality word
    OR a bare definite singular reference to a member of the set ('the
    excluded node', 'the known-red test', 'that node') — which asserts the
    same thing without a numeral and is invisible to a cardinality filter
    alone. It only REPORTS, printed untruncated with field and line, every
    count DERIVED from the enumeration and never written into a format
    string, and it always exits 0 regardless of what it finds — a failure
    inside the sweep itself degrades to a "sweep unavailable" line rather
    than a traceback, so the reading cannot change the verdict on any path
    and not merely on every reading. A machine judging whether a sentence
    ASSERTS a count (the defect) rather than merely counting beside a full
    list (permitted) would be a regex adjudicating meaning, the anti-pattern
    this repository refuses. Its value is bounding what a reader must read,
    not deciding it for them.

Accepts a single plan path, on the same CLI shape as check-order-coverage.py,
whose structural half this script mirrors.
"""
from __future__ import annotations

import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

from agentctl.plan import PlanError, load_plan  # noqa: E402

# --- structural half -----------------------------------------------------

_DESELECT_RE = re.compile(r"--deselect(?:\s+|=)(?P<node>\S+?)(?=\s|;|\}|$)")
_GUARD_INVOCATION_RE = re.compile(
    r"pytest\s+(?P<node>\S+::\S+?)\s+-q\s*>\s*/dev/null\s+2>&1;\s*(?P<var>\w+)=\$\?"
)
_GUARD_COMPARISON_RE = re.compile(r"test\s+\$(?P<var>\w+)\s+-eq\s+(?P<val>\d+)")


def _sites(doc) -> list[tuple[str, str]]:
    """Every (label, command) whose command contains `--deselect`, labelled on
    the same grammar check-order-coverage.py's resolve_control accepts."""
    out = []
    for stage in doc.stages:
        cmd = stage.criterion.verify_command
        if cmd and "--deselect" in cmd:
            out.append((f"stage {stage.index} verify_command", cmd))
    for i, fc in enumerate(doc.meta.final_check, start=1):
        if fc.command and "--deselect" in fc.command:
            out.append((f"final_check {i}", fc.command))
    return out


def _deselected_nodes(command: str) -> list[str]:
    return [m.group("node") for m in _DESELECT_RE.finditer(command)]


def _reaches_deselect_conjunctively(command: str, start: int, first_deselect: int) -> bool:
    """Whether everything between a guard comparison ending at `start` and the
    deselecting run is an `&&` chain. A comparison whose result a `;` discards,
    or a `||` diverts, does not decide whether the deselecting run happens, so
    it proves nothing about the node — textual precedence is not the property."""
    if start > first_deselect:
        return False
    between = command[start:first_deselect]
    return between.lstrip().startswith("&&") and ";" not in between and "||" not in between


def _guarded_nodes(command: str) -> set[str]:
    """Node ids whose exit status is captured into a variable and compared to
    exactly 1, in a conjunct reaching the first `--deselect` in `command`
    through `&&`."""
    first_deselect = command.find("--deselect")
    prefix = command if first_deselect == -1 else command[:first_deselect]
    captured = {
        m.group("var"): m.group("node") for m in _GUARD_INVOCATION_RE.finditer(prefix)
    }
    guarded = set()
    for m in _GUARD_COMPARISON_RE.finditer(prefix):
        if m.group("val") != "1":
            continue
        node = captured.get(m.group("var"))
        if node is None:
            continue
        if not _reaches_deselect_conjunctively(command, m.end(), first_deselect):
            continue
        guarded.add(node)
    return guarded


def structural_violations(doc) -> list[str]:
    """Every way the plan's `--deselect` sites fail the structural property.
    [] == clean, including a plan declaring no such sites at all."""
    sites = _sites(doc)
    if not sites:
        return []

    per_site = {
        label: (frozenset(_deselected_nodes(cmd)), _guarded_nodes(cmd))
        for label, cmd in sites
    }
    union: frozenset[str] = frozenset().union(*(nodes for nodes, _ in per_site.values()))

    out: list[str] = []
    for label, cmd in sorted(sites):
        occurrences = cmd.count("--deselect")
        named = len(_deselected_nodes(cmd))
        if named != occurrences:
            out.append(
                f"{label}: {occurrences} `--deselect` occurrence(s) but only "
                f"{named} name a node this resolver can read — an unrecognised "
                f"spelling is a failure, never a silent skip"
            )
    for label, (nodes, _guarded) in sorted(per_site.items()):
        if nodes != union:
            out.append(
                f"{label}: deselects {sorted(nodes)}, which disagrees with the "
                f"union of every --deselect site's node set {sorted(union)}"
            )
    for label, (nodes, guarded) in sorted(per_site.items()):
        for node in sorted(nodes - guarded):
            out.append(
                f"{label}: deselects {node!r} without a guard proving it exits "
                f"1 in a conjunct reaching the deselecting run through `&&`"
            )
        for node in sorted(guarded - nodes):
            out.append(f"{label}: guards {node!r} (proven exit 1) but never deselects it")
    return out


# --- semantic half ---------------------------------------------------------

_VOCAB_RE = re.compile(
    r"\b(exception set|toleran\w*|deselect\w*|exclud\w*|known-red|inherited-red|"
    r"trunk-red|still-red)\b",
    re.IGNORECASE,
)
_CARDINALITY_RE = re.compile(
    r"\b(\d+|zero|one|two|three|four|five|six|seven|eight|nine|ten|eleven|twelve)\b",
    re.IGNORECASE,
)
_DEFINITE_SINGULAR_RE = re.compile(
    r"\b(the|that|this)\s+(?:[\w-]+\s+){0,3}(node|test)\b", re.IGNORECASE
)
_SENTENCE_SPLIT_RE = re.compile(r"(?<=[.!?])\s+(?=[A-Z\"‘“])")

# One physical line per field is this plan's own convention (no `"""` string
# anywhere in it) — verified before writing this scanner rather than assumed.
_FIELD_LINE_RE = re.compile(r'^(?P<key>[A-Za-z_][\w.]*)\s*=\s*"(?P<value>.*)"\s*$')
_TABLE_HEADER_RE = re.compile(r"^\[(?P<array>\[)?(?P<name>[\w.]+)\]\]?\s*$")
_INDEX_LINE_RE = re.compile(r"^index\s*=\s*(?P<n>\d+)\s*$")


def _unescape(value: str) -> str:
    return value.replace('\\"', '"').replace("\\\\", "\\").replace("\\n", " ")


def _field_sentences(plan_path: Path) -> list[tuple[str, int, str]]:
    """Every (field label, line number, sentence) across every string field
    declared directly in the plan's source text — scanned from the raw lines
    rather than the parsed doc, since only the raw text carries line numbers.

    A stage's own subtables (`[stage.principle]`, `[stage.landed]`) and every
    `[[final_check]]` block keep the ordinal that names WHICH stage or WHICH
    final_check they belong to — dropping it would still report an accurate
    line number, but a reader could no longer tell the field apart from its
    same-named sibling on a different stage/final_check without cross-checking
    that line by hand, defeating "bounding what a reader must read"."""
    table = "meta"
    stage_index: int | None = None
    final_check_index = 0
    out: list[tuple[str, int, str]] = []
    for lineno, line in enumerate(plan_path.read_text(encoding="utf-8").splitlines(), 1):
        header = _TABLE_HEADER_RE.match(line)
        if header:
            table = header.group("name")
            if header.group("array"):
                if table == "stage":
                    stage_index = None
                elif table == "final_check":
                    final_check_index += 1
            continue
        idx = _INDEX_LINE_RE.match(line)
        if idx and table == "stage":
            stage_index = int(idx.group("n"))
            continue
        field = _FIELD_LINE_RE.match(line)
        if not field:
            continue
        if table == "stage" or table.startswith("stage."):
            suffix = table[len("stage"):]
            label = f"stage {stage_index}{suffix}.{field.group('key')}"
        elif table == "final_check" or table.startswith("final_check."):
            suffix = table[len("final_check"):]
            label = f"final_check {final_check_index}{suffix}.{field.group('key')}"
        else:
            label = f"{table}.{field.group('key')}"
        text = _unescape(field.group("value"))
        for sentence in _SENTENCE_SPLIT_RE.split(text):
            sentence = sentence.strip()
            if sentence:
                out.append((label, lineno, sentence))
    return out


def semantic_report(plan_path: Path) -> list[tuple[str, int, str]]:
    """Every (field, line, sentence) carrying exception-set vocabulary together
    with a cardinality word or a bare definite singular reference. Report-only
    — the caller must never let this list affect an exit code."""
    hits = []
    for label, lineno, sentence in _field_sentences(plan_path):
        if not _VOCAB_RE.search(sentence):
            continue
        if _CARDINALITY_RE.search(sentence) or _DEFINITE_SINGULAR_RE.search(sentence):
            hits.append((label, lineno, sentence))
    return hits


def main(argv: list[str]) -> int:
    if len(argv) != 2:
        print(f"usage: {Path(argv[0]).name} <plan.toml>", file=sys.stderr)
        return 2
    plan_path = argv[1]
    try:
        doc = load_plan(plan_path)
    except (OSError, PlanError) as exc:
        print(f"cannot load {plan_path!r}: {exc}", file=sys.stderr)
        return 1

    violations = structural_violations(doc)

    # Blanket except: the sweep re-reads the plan's raw text independently of
    # load_plan, and "the reading can never change the verdict" has to hold on
    # every path — a decode error or an I/O race inside a report-only half must
    # not become this process's exit code, nor suppress the FAIL block below.
    try:
        hits = semantic_report(Path(plan_path))
    except Exception as exc:
        print(
            f"SEMANTIC SWEEP — unavailable for {plan_path} "
            f"({exc.__class__.__name__}: {exc}); report-only, verdict unaffected"
        )
    else:
        print(f"SEMANTIC SWEEP — {len(hits)} candidate sentence(s) to read in {plan_path}:")
        for label, lineno, sentence in hits:
            print(f"  {label} (line {lineno}): {sentence}")

    if violations:
        print(
            f"FAIL — {len(violations)} exception-set problem(s) in {plan_path}:",
            file=sys.stderr,
        )
        for v in violations:
            print(f"  - {v}", file=sys.stderr)
        return 1

    sites = _sites(doc)
    all_nodes = sorted(frozenset().union(*(_deselected_nodes(c) for _, c in sites))) if sites else []
    print(
        f"OK — {plan_path}: {len(sites)} --deselect site(s), "
        f"{len(all_nodes)} node(s), all identical and individually guarded"
    )
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
